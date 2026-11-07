"""Audited ZIP exports. Quarantined records retain original values."""

from __future__ import annotations

import csv
import io
import json
import zipfile
from collections.abc import Mapping
from typing import Any, Final, Literal, TypedDict

from .core import RowRecord, ScanResult

TableName = Literal["clean", "quarantine"]

_FORMULA_PREFIXES: Final[tuple[str, ...]] = ("=", "+", "-", "@")
_REPORT_NOTES: Final[list[str]] = [
    "Input remains unchanged. Row numbers refer to data rows, not physical file lines.",
    "Decimal numbers are canonical strings in JSON to preserve exact precision.",
    "CSV formula escaping is export-only and recorded separately.",
    "Validation proves only the configured schema and key rules, not real-world correctness.",
]


class ExportChange(TypedDict):
    table: TableName
    row: int
    column: str
    before: str
    after: str
    reason: Literal["spreadsheet_formula_escape"]


def _escape_csv_cell(
    value: Any,
    row: int,
    column: str,
    *,
    spreadsheet_safe: bool,
    numeric: bool,
    table: TableName,
    export_changes: list[ExportChange],
) -> str:
    text = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
    if spreadsheet_safe and not numeric and text.lstrip().startswith(_FORMULA_PREFIXES):
        escaped = "'" + text
        export_changes.append(
            {
                "table": table,
                "row": row,
                "column": column,
                "before": text,
                "after": escaped,
                "reason": "spreadsheet_formula_escape",
            }
        )
        return escaped
    return text


def _render_csv_table(
    records: list[RowRecord],
    columns: list[str],
    *,
    clean: bool,
    spreadsheet_safe: bool,
    numeric_columns: frozenset[str],
    export_changes: list[ExportChange],
) -> str:
    table_name: TableName = "clean" if clean else "quarantine"
    with io.StringIO(newline="") as output:
        writer = csv.writer(output, lineterminator="\n")
        headers = [
            _escape_csv_cell(
                column,
                0,
                column,
                spreadsheet_safe=spreadsheet_safe,
                numeric=False,
                table=table_name,
                export_changes=export_changes,
            )
            for column in columns
        ]
        writer.writerow(headers)
        for record in records:
            values = record["proposed"] if clean else record["original"]
            writer.writerow(
                [
                    _escape_csv_cell(
                        values.get(column),
                        record["row"],
                        column,
                        spreadsheet_safe=spreadsheet_safe,
                        numeric=clean and column in numeric_columns,
                        table=table_name,
                        export_changes=export_changes,
                    )
                    for column in columns
                ]
            )
        return output.getvalue()


def _build_zip_archive(files: Mapping[str, str]) -> bytes:
    with io.BytesIO() as buffer:
        with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
            for filename, contents in files.items():
                archive.writestr(filename, contents.encode("utf-8"))
        return buffer.getvalue()


def bundle(result: ScanResult | Mapping[str, Any]) -> bytes:
    export_changes: list[ExportChange] = []
    spreadsheet_safe = bool(result["options"]["spreadsheet_safe"])
    schema_columns: Mapping[str, Any] = result["schema"].get("columns", {})
    numeric_columns = frozenset(
        column
        for column, rule in schema_columns.items()
        if isinstance(rule, Mapping) and rule.get("type") in ("number", "integer")
    )
    records: list[RowRecord] = list(result["records"])
    accepted = [record for record in records if record["status"] == "accepted"]
    quarantine = [record for record in records if record["status"] == "quarantine"]

    clean_csv = _render_csv_table(
        accepted,
        list(result["columns"]),
        clean=True,
        spreadsheet_safe=spreadsheet_safe,
        numeric_columns=numeric_columns,
        export_changes=export_changes,
    )
    quarantine_csv = _render_csv_table(
        quarantine,
        list(result["source_columns"]),
        clean=False,
        spreadsheet_safe=spreadsheet_safe,
        numeric_columns=numeric_columns,
        export_changes=export_changes,
    )

    report = {
        **dict(result),
        "export_changes": export_changes,
        "notes": list(_REPORT_NOTES),
    }
    files: dict[str, str] = {
        "data.clean.csv": clean_csv,
        "data.quarantine.csv": quarantine_csv,
        "data.clean.json": json.dumps([record["proposed"] for record in accepted], ensure_ascii=False, indent=2),
        "data.quarantine.json": json.dumps(quarantine, ensure_ascii=False, indent=2),
        "schema.json": json.dumps(result["schema"], ensure_ascii=False, indent=2),
        "report.json": json.dumps(report, ensure_ascii=False, indent=2),
    }
    return _build_zip_archive(files)
