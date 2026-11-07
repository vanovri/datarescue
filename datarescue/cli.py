"""CLI with explicit export approval and non-destructive file creation."""

from __future__ import annotations

import argparse
import json
import logging
import sys
from collections.abc import Sequence
from pathlib import Path

from pydantic import ValidationError

from .core import MAX_BYTES, DataError, Options, ScanResult, Schema, scan
from .export import bundle

logger = logging.getLogger(__name__)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="datarescue",
        description="Preview and repair flat CSV/JSON data locally.",
    )
    parser.add_argument("command", choices=["scan", "repair"])
    parser.add_argument("file", type=Path)
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--locale", choices=["id-ID", "en-US"], default="id-ID")
    parser.add_argument("--keep-whitespace", action="store_true")
    parser.add_argument(
        "--allow-formulas",
        action="store_true",
        help="Disable spreadsheet formula escaping in CSV exports.",
    )
    parser.add_argument("--json", action="store_true", help="Print the full scan report as JSON.")
    parser.add_argument(
        "--out",
        type=Path,
        help="New ZIP destination for repair; existing files are never overwritten.",
    )
    parser.add_argument(
        "--approve",
        action="store_true",
        help="Approve the configured transformations and quarantine policy.",
    )
    return parser


def _read_input_bytes(path: Path) -> bytes:
    if path.stat().st_size > MAX_BYTES:
        raise DataError("File exceeds 2 MiB.")
    return path.read_bytes()


def _load_schema(schema_path: Path | None) -> Schema:
    if schema_path is None:
        return Schema()
    if schema_path.stat().st_size > MAX_BYTES:
        raise DataError("Schema file exceeds 2 MiB.")
    try:
        raw_schema = schema_path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataError("Schema file must be UTF-8.") from exc
    return Schema.model_validate_json(raw_schema)


def _write_repair_archive(
    source_path: Path,
    out_path: Path | None,
    *,
    approved: bool,
    result: ScanResult,
) -> Path:
    if not approved or out_path is None:
        raise DataError("Repair needs --approve and --out results.zip. Review scan first.")
    if out_path.resolve() == source_path.resolve():
        raise DataError("Output cannot replace the input file.")
    with out_path.open("xb") as output:
        output.write(bundle(result))
    return out_path


def _emit_report(result: ScanResult, *, as_json: bool, exported_to: Path | None) -> None:
    if as_json:
        print(json.dumps(result, ensure_ascii=True, indent=2))
        return
    summary = result["summary"]
    print(
        f"{result['filename']}: {summary['total']} rows; {summary['accepted']} accepted; "
        f"{summary['quarantined']} quarantined; {summary['applied_changes']} approved-row changes."
    )
    if exported_to is not None:
        print(f"Exported: {exported_to}. Original input unchanged.")


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        payload = _read_input_bytes(args.file)
        schema = _load_schema(args.schema)
        options = Options(
            locale=args.locale,
            trim=not args.keep_whitespace,
            spreadsheet_safe=not args.allow_formulas,
        )
        result = scan(payload, args.file.name, schema, options)
        exported_to: Path | None = None
        if args.command == "repair":
            exported_to = _write_repair_archive(
                args.file,
                args.out,
                approved=args.approve,
                result=result,
            )
        _emit_report(result, as_json=args.json, exported_to=exported_to)
        return 1 if result["summary"]["quarantined"] else 0
    except (OSError, UnicodeDecodeError, DataError, ValidationError) as exc:
        logger.debug("CLI execution failed: %s", exc)
        print(f"datarescue: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
