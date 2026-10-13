"""Audited ZIP exports. Quarantined records retain original values."""

import csv
import io
import json
import zipfile


def bundle(result: dict) -> bytes:
    export_changes = []

    def safe(value, row, column, numeric=False, table="clean"):
        text = "" if value is None else str(value).lower() if isinstance(value, bool) else str(value)
        if result["options"]["spreadsheet_safe"] and not numeric and text.lstrip().startswith(("=", "+", "-", "@")):
            export_changes.append({"table": table, "row": row, "column": column, "before": text, "after": "'" + text,
                                   "reason": "spreadsheet_formula_escape"})
            return "'" + text
        return text

    def table(records, clean):
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\n")
        table_name = "clean" if clean else "quarantine"
        columns = result["columns"] if clean else result["source_columns"]
        headers = [safe(column, 0, column, table=table_name) for column in columns]
        writer.writerow(headers)
        for record in records:
            values = record["proposed"] if clean else record["original"]
            writer.writerow([safe(values.get(column), record["row"], column,
                                  numeric=clean and result["schema"]["columns"].get(column, {}).get("type") in ("number", "integer"),
                                  table=table_name) for column in columns])
        return output.getvalue()

    accepted = [record for record in result["records"] if record["status"] == "accepted"]
    quarantine = [record for record in result["records"] if record["status"] == "quarantine"]
    files = {"data.clean.csv": table(accepted, True), "data.quarantine.csv": table(quarantine, False),
             "data.clean.json": json.dumps([record["proposed"] for record in accepted], ensure_ascii=False, indent=2),
             "data.quarantine.json": json.dumps(quarantine, ensure_ascii=False, indent=2),
             "schema.json": json.dumps(result["schema"], ensure_ascii=False, indent=2)}
    report = {**result, "export_changes": export_changes,
              "notes": ["Input remains unchanged. Row numbers refer to data rows, not physical file lines.",
                        "Decimal numbers are canonical strings in JSON to preserve exact precision.",
                        "CSV formula escaping is export-only and recorded separately.",
                        "Validation proves only the configured schema and key rules, not real-world correctness."]}
    files["report.json"] = json.dumps(report, ensure_ascii=False, indent=2)
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for filename, contents in files.items():
            archive.writestr(filename, contents.encode("utf-8"))
    return buffer.getvalue()
