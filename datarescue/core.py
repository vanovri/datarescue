"""Conservative schema-driven repair. Input is never modified or persisted."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

MAX_BYTES = 2 * 1024 * 1024
MAX_ROWS = 10_000
MAX_COLUMNS = 100


class DataError(ValueError):
    """An input cannot be interpreted without losing information."""


class JsonNumber(str):
    """Retain JSON's numeric grammar separately from locale-formatted text."""


class ColumnRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: Literal["string", "integer", "number", "date", "boolean"] = "string"
    required: bool = False
    date_format: Literal["iso", "dmy", "mdy", "auto"] = "iso"


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, ColumnRule] = Field(default_factory=dict, max_length=MAX_COLUMNS)
    key: list[str] = Field(default_factory=list, max_length=10)
    rename: dict[str, str] = Field(default_factory=dict, max_length=MAX_COLUMNS)

    @model_validator(mode="after")
    def check_keys(self):
        if len(set(self.key)) != len(self.key) or any(k not in self.columns for k in self.key):
            raise ValueError("Unique key columns must appear once and be declared in the schema.")
        if any(not source.strip() or not target.strip() or len(target) > 200 for source, target in self.rename.items()):
            raise ValueError("Header mappings need non-empty names of at most 200 characters.")
        return self


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: Literal["id-ID", "en-US"] = "id-ID"
    trim: bool = True
    spreadsheet_safe: bool = True


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"Duplicate JSON field: {key}. Nothing has been discarded.")
        result[key] = value
    return result


def parse(data: bytes, filename: str) -> tuple[list[str], list[dict], str, int]:
    if not data or len(data) > MAX_BYTES:
        raise DataError("Use a non-empty file of at most 2 MiB.")
    try:
        text = data.decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        raise DataError("The file must be UTF-8. Convert its encoding before importing.") from exc
    if "\x00" in text:
        raise DataError("NUL bytes are not supported.")
    blank_records = 0
    if filename.lower().endswith(".json"):
        try:
            rows = json.loads(text, object_pairs_hook=_unique_object, parse_float=JsonNumber,
                              parse_constant=lambda x: (_ for _ in ()).throw(DataError(f"Invalid number: {x}")))
        except json.JSONDecodeError as exc:
            raise DataError(f"Invalid JSON at line {exc.lineno}.") from exc
        except ValueError as exc:
            if isinstance(exc, DataError):
                raise
            raise DataError("JSON contains an unsupported numeric value.") from exc
        except RecursionError as exc:
            raise DataError("Nested JSON is unsupported.") from exc
        if not isinstance(rows, list) or not rows or any(not isinstance(row, dict) for row in rows):
            raise DataError("JSON must contain a non-empty array of flat objects.")
        if any(isinstance(value, (dict, list)) for row in rows for value in row.values()):
            raise DataError("Nested JSON is unsupported. Flatten it explicitly before importing.")
        if any(not row for row in rows):
            raise DataError("Each JSON row must contain at least one field.")
        columns = list(dict.fromkeys(key for row in rows for key in row))
        delimiter = "json"
    elif filename.lower().endswith((".csv", ".tsv")):
        try:
            dialect = csv.Sniffer().sniff(text[:16_384], delimiters=",;\t|")
            delimiter = dialect.delimiter
        except csv.Error:
            delimiter = "\t" if filename.lower().endswith(".tsv") else ","
        try:
            reader = csv.reader(io.StringIO(text, newline=""), delimiter=delimiter, strict=True)
            columns = next(reader, [])
            if len(columns) != len(set(columns)):
                raise DataError("Duplicate CSV headers would lose data. Rename them before importing.")
            rows = []
            for values in reader:
                if not values:
                    blank_records += 1
                    continue
                if len(values) != len(columns):
                    raise DataError(f"Data row {len(rows) + 1} has {len(values)} fields; expected {len(columns)}.")
                rows.append(dict(zip(columns, values)))
                if len(rows) > MAX_ROWS:
                    raise DataError("Use at most 10,000 data rows.")
        except csv.Error as exc:
            raise DataError(f"Malformed CSV: {exc}.") from exc
    else:
        raise DataError("Supported extensions: .csv, .tsv, .json.")
    if not rows or len(rows) > MAX_ROWS:
        raise DataError("Use between 1 and 10,000 data rows.")
    if not columns or len(columns) > MAX_COLUMNS or any(not col.strip() for col in columns):
        raise DataError("Use between 1 and 100 non-empty column names.")
    if any(len(col) > 200 for col in columns):
        raise DataError("Column names must be at most 200 characters.")
    return columns, rows, delimiter, blank_records


def _number(value: Any, locale: str) -> str:
    if isinstance(value, bool):
        raise DataError("Boolean values are not numbers.")
    if isinstance(value, int):
        if len(str(value)) > 100:
            raise DataError("Numbers are limited to 100 digits.")
        return str(value)
    text = str(value)
    if len(text) > 100:
        raise DataError("Numbers are limited to 100 characters.")
    pattern = (r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?" if locale == "id-ID"
               else r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?")
    if not isinstance(value, JsonNumber) and not re.fullmatch(pattern, text):
        raise DataError(f"Invalid number for {locale}. Currency symbols and scientific notation are unsupported.")
    cleaned = text if isinstance(value, JsonNumber) else text.replace(".", "").replace(",", ".") if locale == "id-ID" else text.replace(",", "")
    try:
        number = Decimal(cleaned)
        if not number.is_finite() or abs(number.adjusted()) > 100 or abs(number.as_tuple().exponent) > 100:
            raise InvalidOperation
        canonical = format(number, "f")
        if "." in canonical:
            canonical = canonical.rstrip("0").rstrip(".")
        return "0" if number == 0 else canonical
    except InvalidOperation as exc:
        raise DataError("Invalid decimal.") from exc


def _date(value: Any, date_format: str) -> str:
    text = str(value)
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise DataError("Invalid calendar date.") from exc
    if date_format == "auto":
        candidates = set()
        for pattern in ("%d/%m/%Y", "%m/%d/%Y"):
            try:
                candidates.add(datetime.strptime(text, pattern).date().isoformat())
            except ValueError:
                pass
        if len(candidates) == 1:
            return candidates.pop()
        raise DataError("Ambiguous or invalid date. Choose DMY or MDY explicitly.")
    pattern = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y"}.get(date_format)
    if not pattern:
        raise DataError("Expected an ISO date (YYYY-MM-DD).")
    try:
        return datetime.strptime(text, pattern).date().isoformat()
    except ValueError as exc:
        raise DataError("Invalid calendar date or selected format.") from exc


def convert(value: Any, rule: ColumnRule, options: Options) -> Any:
    if rule.type == "string":
        return str(value).lower() if isinstance(value, bool) else str(value)
    if rule.type in ("number", "integer"):
        canonical = _number(value, options.locale)
        if rule.type == "integer":
            if "." in canonical:
                raise DataError("Expected a whole integer.")
            return int(canonical)
        return canonical  # Exact decimal text, never binary float.
    if rule.type == "date":
        return _date(value, rule.date_format)
    if isinstance(value, bool):
        return value
    text = str(value).lower()
    if text in ("true", "false"):
        return text == "true"
    raise DataError("Expected true or false. Values such as 1, yes and aktif are not guessed.")


def scan(data: bytes, filename: str, schema: Schema | None = None, options: Options | None = None) -> dict:
    schema, options = schema or Schema(), options or Options()
    source_columns, originals, delimiter, blank_records = parse(data, filename)
    if any(source not in source_columns for source in schema.rename):
        raise DataError("A header mapping refers to a missing source column.")
    columns = [schema.rename.get(column, column) for column in source_columns]
    if len(columns) != len(set(columns)):
        raise DataError("Header mappings would merge columns and lose data. Choose unique target names.")
    source_for = dict(zip(columns, source_columns))
    drift = []
    for name in schema.columns:
        if name not in columns:
            drift.append({"code": "missing_column", "column": name, "severity": "error"})
    if schema.columns:
        for name in columns:
            if name not in schema.columns:
                drift.append({"code": "extra_column", "column": name, "severity": "info"})
    seen_keys = set()
    records = []
    for row_number, original in enumerate(originals, 1):
        mapped = {schema.rename.get(name, name): value for name, value in original.items()}
        cleaned, errors, changes = dict(mapped), [], []
        for name in columns:
            before = mapped.get(name)
            value = before
            if isinstance(value, str) and not isinstance(value, JsonNumber) and options.trim:
                value = value.strip()
            rule = schema.columns.get(name)
            empty = value is None or value == ""
            if rule and empty:
                if rule.required:
                    errors.append({"column": name, "code": "required", "message": "A value is required."})
                elif rule.type != "string":
                    value = None
            elif rule:
                try:
                    value = convert(value, rule, options)
                except DataError as exc:
                    errors.append({"column": name, "code": "invalid_type", "message": str(exc)})
            cleaned[name] = value
            if value != before or type(value) is not type(before):
                changes.append({"column": name, "source_column": source_for[name], "before": before, "after": value})
        for name in schema.columns:
            if name not in mapped:
                errors.append({"column": name, "code": "missing_field", "message": "A contracted field is missing."})
        if schema.key:
            key_values = tuple(cleaned.get(name) for name in schema.key)
            if any(value is None or value == "" for value in key_values):
                errors.append({"column": ", ".join(schema.key), "code": "empty_key", "message": "Unique keys cannot be empty."})
            else:
                # Tag each value's type so true, 1 and "1" are distinct.
                key = tuple((type(value).__name__, str(value)) for value in key_values)
                if key in seen_keys:
                    errors.append({"column": ", ".join(schema.key), "code": "duplicate_key", "message": "An earlier accepted row has this key."})
                elif not errors:
                    seen_keys.add(key)
        records.append({"row": row_number, "original": original, "proposed": cleaned,
                        "status": "quarantine" if errors else "accepted", "errors": errors, "changes": changes})
    accepted = sum(record["status"] == "accepted" for record in records)
    digest = hashlib.sha256(data).hexdigest()
    plan_id = hashlib.sha256(json.dumps({"source": digest, "schema": schema.model_dump(), "options": options.model_dump()}, sort_keys=True).encode()).hexdigest()
    return {"filename": filename.replace("\\", "/").split("/")[-1],
            "sha256": digest, "plan_id": plan_id, "bytes": len(data), "delimiter": delimiter, "blank_records": blank_records,
            "columns": columns, "source_columns": source_columns,
            "header_changes": [{"before": source, "after": target} for source, target in schema.rename.items() if source != target],
            "schema": schema.model_dump(), "options": options.model_dump(),
            "drift": drift, "records": records,
            "summary": {"total": len(records), "accepted": accepted, "quarantined": len(records) - accepted,
                        "proposed_changes": sum(len(record["changes"]) for record in records),
                        "applied_changes": sum(len(record["changes"]) for record in records if record["status"] == "accepted"),
                        "errors": sum(len(record["errors"]) for record in records)}}
