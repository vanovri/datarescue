"""CLI with explicit export approval and non-destructive file creation."""

import argparse
import json
import sys
from pathlib import Path

from pydantic import ValidationError

from .core import MAX_BYTES, DataError, Options, Schema, scan
from .export import bundle


def main(argv=None):
    parser = argparse.ArgumentParser(prog="datarescue", description="Preview and repair flat CSV/JSON data locally.")
    parser.add_argument("command", choices=["scan", "repair"])
    parser.add_argument("file", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--locale", choices=["id-ID", "en-US"], default="id-ID")
    parser.add_argument("--keep-whitespace", action="store_true")
    parser.add_argument("--allow-formulas", action="store_true", help="Disable spreadsheet formula escaping in CSV exports.")
    parser.add_argument("--json", action="store_true", help="Print the full scan report as JSON.")
    parser.add_argument("--out", type=Path, help="New ZIP destination for repair; existing files are never overwritten.")
    parser.add_argument("--approve", action="store_true", help="Approve the configured transformations and quarantine policy.")
    args = parser.parse_args(argv)
    try:
        if args.file.stat().st_size > MAX_BYTES:
            raise DataError("File exceeds 2 MiB.")
        schema = Schema.model_validate_json(args.schema.read_text(encoding="utf-8-sig")) if args.schema else Schema()
        options = Options(locale=args.locale, trim=not args.keep_whitespace, spreadsheet_safe=not args.allow_formulas)
        result = scan(args.file.read_bytes(), args.file.name, schema, options)
        if args.command == "repair":
            if not args.approve or not args.out:
                raise DataError("Repair needs --approve and --out results.zip. Review scan first.")
            if args.out.resolve() == args.file.resolve():
                raise DataError("Output cannot replace the input file.")
            with args.out.open("xb") as output:
                output.write(bundle(result))
        if args.json:
            print(json.dumps(result, ensure_ascii=True, indent=2))
        else:
            summary = result["summary"]
            print(f"{result['filename']}: {summary['total']} rows; {summary['accepted']} accepted; "
                  f"{summary['quarantined']} quarantined; {summary['applied_changes']} approved-row changes.")
            if args.command == "repair":
                print(f"Exported: {args.out}. Original input unchanged.")
        return 1 if result["summary"]["quarantined"] else 0
    except (OSError, DataError, ValidationError) as exc:
        print(f"datarescue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
