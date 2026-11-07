"""Conservative schema-driven repair. Input is never modified or persisted."""

from __future__ import annotations

import csv
import hashlib
import io
import json
import logging
import re
from datetime import date, datetime
from decimal import Decimal, InvalidOperation
from typing import Any, Final, Literal, TypeAlias, TypedDict

from pydantic import BaseModel, ConfigDict, Field, model_validator

logger = logging.getLogger(__name__)

MAX_BYTES: Final[int] = 2 * 1024 * 1024
MAX_ROWS: Final[int] = 10_000
MAX_COLUMNS: Final[int] = 100
MAX_COLUMN_NAME_LENGTH: Final[int] = 200
MAX_NUMERIC_DIGITS: Final[int] = 100

ColumnType: TypeAlias = Literal["string", "integer", "number", "date", "boolean"]
DateFormat: TypeAlias = Literal["iso", "dmy", "mdy", "auto"]
LocaleCode: TypeAlias = Literal["id-ID", "en-US"]
DriftCode: TypeAlias = Literal["missing_column", "extra_column"]
DriftSeverity: TypeAlias = Literal["error", "info"]
RowStatus: TypeAlias = Literal["accepted", "quarantine"]
CompositeKey: TypeAlias = tuple[tuple[str, str], ...]

_ID_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:\.\d{3})+)(?:,\d+)?")
_US_NUMBER_RE: Final[re.Pattern[str]] = re.compile(r"[+-]?(?:\d+|\d{1,3}(?:,\d{3})+)(?:\.\d+)?")
_ISO_DATE_RE: Final[re.Pattern[str]] = re.compile(r"\d{4}-\d{2}-\d{2}")
_EXPLICIT_DATE_FORMATS: Final[dict[str, str]] = {"dmy": "%d/%m/%Y", "mdy": "%m/%d/%Y"}
_AUTO_DATE_FORMATS: Final[tuple[str, str]] = ("%d/%m/%Y", "%m/%d/%Y")


class HeaderChange(TypedDict):
    before: str
    after: str


class DriftItem(TypedDict):
    code: DriftCode
    column: str
    severity: DriftSeverity


class CellError(TypedDict):
    column: str
    code: str
    message: str


class CellChange(TypedDict):
    column: str
    source_column: str
    before: Any
    after: Any


class RowRecord(TypedDict):
    row: int
    original: dict[str, Any]
    proposed: dict[str, Any]
    status: RowStatus
    errors: list[CellError]
    changes: list[CellChange]


class ScanSummary(TypedDict):
    total: int
    accepted: int
    quarantined: int
    proposed_changes: int
    applied_changes: int
    errors: int


class ScanResult(TypedDict):
    filename: str
    sha256: str
    plan_id: str
    bytes: int
    delimiter: str
    blank_records: int
    columns: list[str]
    source_columns: list[str]
    header_changes: list[HeaderChange]
    schema: dict[str, Any]
    options: dict[str, Any]
    drift: list[DriftItem]
    records: list[RowRecord]
    summary: ScanSummary


class DataError(ValueError):
    """An input cannot be interpreted without losing information."""


class JsonNumber(str):
    """Retain JSON's numeric grammar separately from locale-formatted text."""


class ColumnRule(BaseModel):
    model_config = ConfigDict(extra="forbid")
    type: ColumnType = "string"
    required: bool = False
    date_format: DateFormat = "iso"


class Schema(BaseModel):
    model_config = ConfigDict(extra="forbid")
    columns: dict[str, ColumnRule] = Field(default_factory=dict, max_length=MAX_COLUMNS)
    key: list[str] = Field(default_factory=list, max_length=10)
    rename: dict[str, str] = Field(default_factory=dict, max_length=MAX_COLUMNS)

    @model_validator(mode="after")
    def check_keys(self) -> Schema:
        if len(set(self.key)) != len(self.key) or any(k not in self.columns for k in self.key):
            raise ValueError("Unique key columns must appear once and be declared in the schema.")
        if any(
            not source.strip() or not target.strip() or len(target) > MAX_COLUMN_NAME_LENGTH
            for source, target in self.rename.items()
        ):
            raise ValueError("Header mappings need non-empty names of at most 200 characters.")
        return self


class Options(BaseModel):
    model_config = ConfigDict(extra="forbid")
    locale: LocaleCode = "id-ID"
    trim: bool = True
    spreadsheet_safe: bool = True


def _unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DataError(f"Duplicate JSON field: {key}. Nothing has been discarded.")
        result[key] = value
    return result


def _reject_json_constant(token: str) -> Any:
    raise DataError(f"Invalid number: {token}")


def _decode_utf8_source(data: bytes) -> str:
    if not isinstance(data, (bytes, bytearray)):
        raise DataError("Input payload must be raw bytes.")
    if not data or len(data) > MAX_BYTES:
        raise DataError("Use a non-empty file of at most 2 MiB.")
    try:
        text = bytes(data).decode("utf-8-sig")
    except UnicodeDecodeError as exc:
        logger.debug("Rejected non-UTF-8 input payload: %s", exc)
        raise DataError("The file must be UTF-8. Convert its encoding before importing.") from exc
    if "\x00" in text:
        raise DataError("NUL bytes are not supported.")
    return text


def _parse_json_records(text: str) -> tuple[list[str], list[dict[str, Any]]]:
    try:
        rows = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_float=JsonNumber,
            parse_constant=_reject_json_constant,
        )
    except json.JSONDecodeError as exc:
        raise DataError(f"Invalid JSON at line {exc.lineno}.") from exc
    except DataError:
        raise
    except ValueError as exc:
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
    return columns, rows


def _sniff_delimiter(text: str, filename: str) -> str:
    try:
        dialect = csv.Sniffer().sniff(text[:16_384], delimiters=",;\t|")
        return dialect.delimiter
    except csv.Error:
        return "\t" if filename.lower().endswith(".tsv") else ","


def _parse_delimited_records(text: str, delimiter: str) -> tuple[list[str], list[dict[str, Any]], int]:
    blank_records = 0
    try:
        with io.StringIO(text, newline="") as stream:
            reader = csv.reader(stream, delimiter=delimiter, strict=True)
            columns = next(reader, [])
            if len(columns) != len(set(columns)):
                raise DataError("Duplicate CSV headers would lose data. Rename them before importing.")
            rows: list[dict[str, Any]] = []
            for values in reader:
                if not values:
                    blank_records += 1
                    continue
                if len(values) != len(columns):
                    raise DataError(
                        f"Data row {len(rows) + 1} has {len(values)} fields; expected {len(columns)}."
                    )
                rows.append(dict(zip(columns, values)))
                if len(rows) > MAX_ROWS:
                    raise DataError("Use at most 10,000 data rows.")
    except csv.Error as exc:
        raise DataError(f"Malformed CSV: {exc}.") from exc
    return columns, rows, blank_records


def _validate_table_shape(columns: list[str], rows: list[dict[str, Any]]) -> None:
    if not rows or len(rows) > MAX_ROWS:
        raise DataError("Use between 1 and 10,000 data rows.")
    if not columns or len(columns) > MAX_COLUMNS or any(not col.strip() for col in columns):
        raise DataError("Use between 1 and 100 non-empty column names.")
    if any(len(col) > MAX_COLUMN_NAME_LENGTH for col in columns):
        raise DataError("Column names must be at most 200 characters.")


def parse(data: bytes, filename: str) -> tuple[list[str], list[dict[str, Any]], str, int]:
    if not isinstance(filename, str) or not filename.strip():
        raise DataError("A non-empty filename is required.")
    text = _decode_utf8_source(data)
    lower_name = filename.lower()
    if lower_name.endswith(".json"):
        columns, rows = _parse_json_records(text)
        delimiter = "json"
        blank_records = 0
    elif lower_name.endswith((".csv", ".tsv")):
        delimiter = _sniff_delimiter(text, lower_name)
        columns, rows, blank_records = _parse_delimited_records(text, delimiter)
    else:
        raise DataError("Supported extensions: .csv, .tsv, .json.")
    _validate_table_shape(columns, rows)
    return columns, rows, delimiter, blank_records


def _canonicalize_decimal(number: Decimal) -> str:
    exponent = number.as_tuple().exponent
    if (
        not number.is_finite()
        or not isinstance(exponent, int)
        or abs(number.adjusted()) > MAX_NUMERIC_DIGITS
        or abs(exponent) > MAX_NUMERIC_DIGITS
    ):
        raise DataError("Invalid decimal.")
    canonical = format(number, "f")
    if "." in canonical:
        canonical = canonical.rstrip("0").rstrip(".")
    return "0" if number == 0 else canonical


def _number(value: Any, locale: LocaleCode) -> str:
    if isinstance(value, bool):
        raise DataError("Boolean values are not numbers.")
    if isinstance(value, float):
        raise DataError("Binary float values are not supported; use exact text or Decimal.")
    if isinstance(value, Decimal):
        return _canonicalize_decimal(value)
    if isinstance(value, int):
        if len(str(abs(value))) > MAX_NUMERIC_DIGITS:
            raise DataError("Numbers are limited to 100 digits.")
        return str(value)
    text = str(value)
    if len(text) > MAX_NUMERIC_DIGITS:
        raise DataError("Numbers are limited to 100 characters.")
    if isinstance(value, JsonNumber):
        cleaned = text
    else:
        pattern = _ID_NUMBER_RE if locale == "id-ID" else _US_NUMBER_RE
        if not pattern.fullmatch(text):
            raise DataError(
                f"Invalid number for {locale}. Currency symbols and scientific notation are unsupported."
            )
        cleaned = text.replace(".", "").replace(",", ".") if locale == "id-ID" else text.replace(",", "")
    try:
        return _canonicalize_decimal(Decimal(cleaned))
    except InvalidOperation as exc:
        raise DataError("Invalid decimal.") from exc


def _try_parse_date(text: str, fmt: str) -> str | None:
    try:
        return datetime.strptime(text, fmt).date().isoformat()
    except ValueError:
        return None


def _date(value: Any, date_format: DateFormat) -> str:
    if isinstance(value, (bool, int, float)):
        raise DataError("Expected a formatted date string.")
    text = str(value)
    if _ISO_DATE_RE.fullmatch(text):
        try:
            return date.fromisoformat(text).isoformat()
        except ValueError as exc:
            raise DataError("Invalid calendar date.") from exc
    if date_format == "auto":
        candidates = {parsed for fmt in _AUTO_DATE_FORMATS if (parsed := _try_parse_date(text, fmt)) is not None}
        if len(candidates) == 1:
            return candidates.pop()
        raise DataError("Ambiguous or invalid date. Choose DMY or MDY explicitly.")
    fmt = _EXPLICIT_DATE_FORMATS.get(date_format)
    if fmt is None:
        raise DataError("Expected an ISO date (YYYY-MM-DD).")
    parsed = _try_parse_date(text, fmt)
    if parsed is None:
        raise DataError("Invalid calendar date or selected format.")
    return parsed


def convert(value: Any, rule: ColumnRule, options: Options) -> Any:
    if value is None:
        raise DataError("Null values cannot be converted.")
    if isinstance(value, str) and not isinstance(value, JsonNumber) and options.trim:
        value = value.strip()
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


def _resolve_headers(
    source_columns: list[str], rename: dict[str, str]
) -> tuple[list[str], dict[str, str], list[HeaderChange]]:
    source_column_set = set(source_columns)
    if any(source not in source_column_set for source in rename):
        raise DataError("A header mapping refers to a missing source column.")
    columns = [rename.get(column, column) for column in source_columns]
    if len(columns) != len(set(columns)):
        raise DataError("Header mappings would merge columns and lose data. Choose unique target names.")
    source_for = dict(zip(columns, source_columns))
    header_changes: list[HeaderChange] = [
        {"before": source, "after": target} for source, target in rename.items() if source != target
    ]
    return columns, source_for, header_changes


def _detect_schema_drift(columns: list[str], schema: Schema) -> list[DriftItem]:
    drift: list[DriftItem] = []
    column_names = set(columns)
    for name in schema.columns:
        if name not in column_names:
            drift.append({"code": "missing_column", "column": name, "severity": "error"})
    if schema.columns:
        for name in columns:
            if name not in schema.columns:
                drift.append({"code": "extra_column", "column": name, "severity": "info"})
    return drift


def _evaluate_cell(
    name: str,
    before: Any,
    source_column: str,
    rule: ColumnRule | None,
    options: Options,
) -> tuple[Any, CellError | None, CellChange | None]:
    value = before
    if isinstance(value, str) and not isinstance(value, JsonNumber) and options.trim:
        value = value.strip()
    error: CellError | None = None
    empty = value is None or value == ""
    if rule and empty:
        if rule.required:
            error = {"column": name, "code": "required", "message": "A value is required."}
        elif rule.type != "string":
            value = None
    elif rule:
        try:
            value = convert(value, rule, options)
        except DataError as exc:
            error = {"column": name, "code": "invalid_type", "message": str(exc)}
    change: CellChange | None = None
    if value != before or type(value) is not type(before):
        change = {"column": name, "source_column": source_column, "before": before, "after": value}
    return value, error, change


def _validate_row_key(
    cleaned: dict[str, Any],
    key_columns: list[str],
    seen_keys: set[CompositeKey],
    has_row_errors: bool,
) -> CellError | None:
    if not key_columns:
        return None
    key_values = tuple(cleaned.get(name) for name in key_columns)
    joined_key_name = ", ".join(key_columns)
    if any(value is None or value == "" for value in key_values):
        return {"column": joined_key_name, "code": "empty_key", "message": "Unique keys cannot be empty."}
    # Tag each value's type so true, 1 and "1" are distinct.
    composite_key: CompositeKey = tuple((type(value).__name__, str(value)) for value in key_values)
    if composite_key in seen_keys:
        return {
            "column": joined_key_name,
            "code": "duplicate_key",
            "message": "An earlier accepted row has this key.",
        }
    if not has_row_errors:
        seen_keys.add(composite_key)
    return None


def _evaluate_row(
    row_number: int,
    original: dict[str, Any],
    columns: list[str],
    source_for: dict[str, str],
    schema: Schema,
    options: Options,
    seen_keys: set[CompositeKey],
) -> RowRecord:
    mapped = {schema.rename.get(name, name): value for name, value in original.items()}
    cleaned: dict[str, Any] = dict(mapped)
    errors: list[CellError] = []
    changes: list[CellChange] = []
    for name in columns:
        value, cell_error, cell_change = _evaluate_cell(
            name,
            mapped.get(name),
            source_for[name],
            schema.columns.get(name),
            options,
        )
        cleaned[name] = value
        if cell_error is not None:
            errors.append(cell_error)
        if cell_change is not None:
            changes.append(cell_change)
    for name in schema.columns:
        if name not in mapped:
            errors.append({"column": name, "code": "missing_field", "message": "A contracted field is missing."})
    key_error = _validate_row_key(cleaned, schema.key, seen_keys, bool(errors))
    if key_error is not None:
        errors.append(key_error)
    status: RowStatus = "quarantine" if errors else "accepted"
    return {
        "row": row_number,
        "original": original,
        "proposed": cleaned,
        "status": status,
        "errors": errors,
        "changes": changes,
    }


def _summarize_records(records: list[RowRecord]) -> ScanSummary:
    total = len(records)
    accepted = sum(1 for record in records if record["status"] == "accepted")
    return {
        "total": total,
        "accepted": accepted,
        "quarantined": total - accepted,
        "proposed_changes": sum(len(record["changes"]) for record in records),
        "applied_changes": sum(
            len(record["changes"]) for record in records if record["status"] == "accepted"
        ),
        "errors": sum(len(record["errors"]) for record in records),
    }


def _compute_plan_id(source_sha256: str, schema: Schema, options: Options) -> str:
    canonical_plan = json.dumps(
        {"source": source_sha256, "schema": schema.model_dump(), "options": options.model_dump()},
        sort_keys=True,
    )
    return hashlib.sha256(canonical_plan.encode("utf-8")).hexdigest()


def scan(
    data: bytes,
    filename: str,
    schema: Schema | None = None,
    options: Options | None = None,
) -> ScanResult:
    active_schema = schema or Schema()
    active_options = options or Options()
    source_columns, originals, delimiter, blank_records = parse(data, filename)
    columns, source_for, header_changes = _resolve_headers(source_columns, active_schema.rename)
    drift = _detect_schema_drift(columns, active_schema)
    seen_keys: set[CompositeKey] = set()
    records = [
        _evaluate_row(
            row_number,
            original,
            columns,
            source_for,
            active_schema,
            active_options,
            seen_keys,
        )
        for row_number, original in enumerate(originals, 1)
    ]
    digest = hashlib.sha256(data).hexdigest()
    plan_id = _compute_plan_id(digest, active_schema, active_options)
    safe_basename = filename.replace("\\", "/").split("/")[-1]
    return {
        "filename": safe_basename,
        "sha256": digest,
        "plan_id": plan_id,
        "bytes": len(data),
        "delimiter": delimiter,
        "blank_records": blank_records,
        "columns": columns,
        "source_columns": source_columns,
        "header_changes": header_changes,
        "schema": active_schema.model_dump(),
        "options": active_options.model_dump(),
        "drift": drift,
        "records": records,
        "summary": _summarize_records(records),
    }
