from __future__ import annotations

import asyncio
import io
import json
import math
import zipfile
from decimal import Decimal
from pathlib import Path
from typing import Any

import httpx
import pytest
from fastapi import HTTPException, UploadFile
from pydantic import ValidationError

import datarescue.api as api_module
from datarescue.api import UploadLimit, analyze, app
from datarescue.cli import main
from datarescue.core import (
    MAX_BYTES,
    MAX_COLUMNS,
    MAX_ROWS,
    ColumnRule,
    DataError,
    JsonNumber,
    Options,
    Schema,
    convert,
    parse,
    scan,
)
from datarescue.export import bundle


class SyncASGIClient:
    def request(self, method: str, url: str, **kwargs: Any) -> httpx.Response:
        async def _send() -> httpx.Response:
            transport = httpx.ASGITransport(app=app)
            async with httpx.AsyncClient(transport=transport, base_url="http://test") as http_client:
                return await http_client.request(method, url, **kwargs)

        return asyncio.run(_send())

    def get(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any) -> httpx.Response:
        return self.request("POST", url, **kwargs)


client = SyncASGIClient()


# ---------------------------------------------------------------------------
# 1. Ingestion: CSV / TSV / JSON / Delimiters / Encodings / Bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("filename", "payload", "expected_delimiter"),
    [
        ("comma.csv", b"a,b\n1,2\n", ","),
        ("semi.csv", b"a;b\n1;2\n", ";"),
        ("pipe.csv", b"a|b\n1|2\n", "|"),
        ("tab.tsv", b"a\tb\n1\t2\n", "\t"),
        ("single_col.csv", b"only_header\nval\n", ","),
        ("single_col.tsv", b"only_header\nval\n", "\t"),
    ],
)
def test_delimiter_detection_and_single_column_fallbacks(
    filename: str, payload: bytes, expected_delimiter: str
) -> None:
    columns, rows, delimiter, blank_records = parse(payload, filename)
    assert delimiter == expected_delimiter
    assert blank_records == 0
    assert len(rows) == 1
    assert len(columns) >= 1


def test_multiline_csv_cells_keep_logical_row_numbering() -> None:
    payload = b'id,note\n001,"first line\nsecond line"\n\n002,"third\nfourth"\n'
    result = scan(payload, "notes.csv", None, Options())
    assert result["blank_records"] == 1
    assert [r["row"] for r in result["records"]] == [1, 2]
    assert result["records"][0]["proposed"]["note"] == "first line\nsecond line"


def test_utf8_bom_and_international_characters_preserved() -> None:
    bom_csv = "\ufeffid,kota\n001,Yogyakarta — Istimewa 🇮🇩\n".encode("utf-8")
    result = scan(bom_csv, "kota.csv")
    assert result["columns"] == ["id", "kota"]
    assert result["records"][0]["proposed"]["kota"] == "Yogyakarta — Istimewa 🇮🇩"

    with zipfile.ZipFile(io.BytesIO(bundle(result))) as archive:
        clean_json = json.loads(archive.read("data.clean.json").decode("utf-8"))
        assert clean_json[0]["kota"] == "Yogyakarta — Istimewa 🇮🇩"


@pytest.mark.parametrize(
    "bad_bytes",
    [
        "id,name\n1,caf\xe9\n".encode("latin-1"),  # raw Latin-1 byte 0xe9
        b"id,name\n1,\x80\x93\n",  # CP1252 smart punctuation bytes invalid in UTF-8
        b"id,name\n1,\xc3\n",  # truncated UTF-8 sequence
    ],
)
def test_non_utf8_or_mojibake_raw_bytes_rejected_without_corruption(bad_bytes: bytes) -> None:
    with pytest.raises(DataError, match="UTF-8"):
        parse(bad_bytes, "broken.csv")


def test_nul_bytes_and_oversized_payload_rejected() -> None:
    with pytest.raises(DataError, match="NUL bytes"):
        parse(b"id,name\n1,abc\x00def\n", "nul.csv")

    with pytest.raises(DataError, match="2 MiB"):
        parse(b"a" * (MAX_BYTES + 1), "huge.csv")


@pytest.mark.parametrize("ext", ["data.jsonl", "data.ndjson", "data.xlsx", "data.parquet", "noext"])
def test_unsupported_extensions_including_jsonl_rejected_explicitly(ext: str) -> None:
    with pytest.raises(DataError, match="Supported extensions"):
        parse(b'{"id": 1}\n', ext)


@pytest.mark.parametrize(
    ("payload", "match"),
    [
        (b"{bad json", "Invalid JSON at line 1"),
        (b"{}", "non-empty array of flat objects"),
        (b"[]", "non-empty array of flat objects"),
        (b"[1, 2, 3]", "non-empty array of flat objects"),
        (b"[{}]", "at least one field"),
        (b'[{"a": [1, 2]}]', "Nested JSON is unsupported"),
        (b'[{"n": Infinity}]', "Invalid number"),
        (b'[{"n": -Infinity}]', "Invalid number"),
    ],
)
def test_json_structural_and_constant_errors(payload: bytes, match: str) -> None:
    with pytest.raises(DataError, match=match):
        parse(payload, "items.json")


def test_deeply_nested_json_recursion_error_is_wrapped(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    deep_object = b'[{"a": ' + (b"[" * 500) + (b"]" * 500) + b"}]"
    with pytest.raises(DataError, match="Nested JSON"):
        parse(deep_object, "deep.json")

    def _raise_recursion(*_args: Any, **_kwargs: Any) -> Any:
        raise RecursionError("maximum recursion depth exceeded")

    monkeypatch.setattr(json, "loads", _raise_recursion)
    with pytest.raises(DataError, match="Nested JSON is unsupported"):
        parse(b'[{"a": 1}]', "deep.json")


def test_json_huge_integer_exceeding_str_digits_limit_is_rejected() -> None:
    huge_int_json = b'[{"n": ' + (b"9" * 5_000) + b"}]"
    with pytest.raises(DataError, match="unsupported numeric value"):
        parse(huge_int_json, "huge_int.json")


def test_parse_rejects_non_bytes_payload_and_blank_filename() -> None:
    with pytest.raises(DataError, match="raw bytes"):
        parse("not-bytes", "items.csv")  # type: ignore[arg-type]
    with pytest.raises(DataError, match="filename"):
        parse(b"id\n1\n", "   ")


def test_json_heterogeneous_objects_preserve_column_discovery_order() -> None:
    payload = b'[{"id": "1", "a": 10}, {"id": "2", "b": 20}]'
    columns, rows, delimiter, blank = parse(payload, "hetero.json")
    assert columns == ["id", "a", "b"]
    assert delimiter == "json"
    assert blank == 0
    assert rows[0] == {"id": "1", "a": 10}
    assert rows[1] == {"id": "2", "b": 20}


def test_malformed_csv_and_table_dimension_bounds() -> None:
    # Strict CSV unclosed quote
    with pytest.raises(DataError, match="Malformed CSV"):
        parse(b'id,name\n1,"unclosed\n', "bad.csv")

    # Header only (0 data rows)
    with pytest.raises(DataError, match="between 1 and 10,000"):
        parse(b"id,name\n", "empty_rows.csv")

    # Exceed MAX_ROWS in CSV
    too_many_csv = ("id\n" + "".join("1\n" for _ in range(MAX_ROWS + 1))).encode("utf-8")
    with pytest.raises(DataError, match="10,000"):
        parse(too_many_csv, "many.csv")

    # Exceed MAX_ROWS in JSON
    too_many_json = ("[" + ",".join('{"a":1}' for _ in range(MAX_ROWS + 1)) + "]").encode("utf-8")
    with pytest.raises(DataError, match="10,000"):
        parse(too_many_json, "many.json")

    # Exceed MAX_COLUMNS
    wide_headers = ",".join(f"c{i}" for i in range(MAX_COLUMNS + 1))
    wide_row = ",".join("1" for _ in range(MAX_COLUMNS + 1))
    with pytest.raises(DataError, match="100 non-empty"):
        parse(f"{wide_headers}\n{wide_row}\n".encode(), "wide.csv")

    # Blank column header
    with pytest.raises(DataError, match="100 non-empty"):
        parse(b"id,   \n1,2\n", "blank_col.csv")

    # Column name > 200 chars
    long_col = "x" * 201
    with pytest.raises(DataError, match="200 characters"):
        parse(f"{long_col}\n1\n".encode(), "long_col.csv")


# ---------------------------------------------------------------------------
# 2. Schema Validation, Header Mapping Collisions & Drift Detection
# ---------------------------------------------------------------------------


def test_schema_validator_rejects_invalid_keys_renames_and_extra_fields() -> None:
    with pytest.raises(ValidationError, match="Unique key"):
        Schema.model_validate({"columns": {"id": {}}, "key": ["id", "id"]})

    with pytest.raises(ValidationError, match="Header mappings"):
        Schema.model_validate({"rename": {"   ": "target"}})

    with pytest.raises(ValidationError, match="Header mappings"):
        Schema.model_validate({"rename": {"src": "x" * 201}})

    with pytest.raises(ValidationError):
        ColumnRule.model_validate({"type": "string", "unknown_flag": True})

    with pytest.raises(ValidationError):
        Options.model_validate({"locale": "fr-FR"})


def test_missing_rename_source_and_extra_vs_missing_drift() -> None:
    with pytest.raises(DataError, match="missing source column"):
        scan(b"a,b\n1,2\n", "x.csv", Schema.model_validate({"rename": {"missing": "c"}}))

    schema = Schema.model_validate(
        {
            "columns": {"id": {"type": "string"}, "expected_col": {"type": "integer"}},
            "rename": {"id": "id"},
        }
    )
    result = scan(b"id,surprise_col\n001,hello\n", "path\\to\\x.csv", schema)
    assert result["filename"] == "x.csv"
    assert result["header_changes"] == []
    assert result["drift"] == [
        {"code": "missing_column", "column": "expected_col", "severity": "error"},
        {"code": "extra_column", "column": "surprise_col", "severity": "info"},
    ]


# ---------------------------------------------------------------------------
# 3. Cell Coercion: Numbers, Currency, Integers, Dates, Booleans & Nulls
# ---------------------------------------------------------------------------


def test_numeric_edge_cases_bool_trap_floats_decimals_and_extremes() -> None:
    num_rule = ColumnRule(type="number")
    int_rule = ColumnRule(type="integer")

    # bool-as-int trap
    for flag in (True, False):
        with pytest.raises(DataError, match="Boolean values are not numbers"):
            convert(flag, num_rule, Options())
        with pytest.raises(DataError, match="Boolean values are not numbers"):
            convert(flag, int_rule, Options())

    # Binary float values (including 1.234 which would collide with id-ID thousands, NaN, Inf)
    for bad_float in (1.234, 0.1, math.nan, math.inf, -math.inf):
        with pytest.raises(DataError):
            convert(bad_float, num_rule, Options(locale="id-ID"))

    # Decimal support & non-finite Decimal rejection
    assert convert(Decimal("1234.500"), num_rule, Options(locale="id-ID")) == "1234.5"
    assert convert(Decimal("-0.00"), num_rule, Options()) == "0"
    for bad_dec in (Decimal("NaN"), Decimal("Infinity"), Decimal("-Infinity")):
        with pytest.raises(DataError):
            convert(bad_dec, num_rule, Options())

    # Currency symbols & scientific notation text rejected
    for currency in ("Rp 10.000", "$100.00", "€50", "1e5", "1,234.56"):
        with pytest.raises(DataError):
            convert(currency, num_rule, Options(locale="id-ID"))

    # Negative zero canonicalization
    assert convert("-0,00", num_rule, Options(locale="id-ID")) == "0"
    assert convert("-0.00", num_rule, Options(locale="en-US")) == "0"

    # Whole integer with trailing zero decimals vs fractional decimal
    assert convert("1.250,00", int_rule, Options(locale="id-ID")) == 1250
    with pytest.raises(DataError, match="whole integer"):
        convert("1.250,50", int_rule, Options(locale="id-ID"))

    # Extreme digit / character limits
    assert convert(10**99, int_rule, Options()) == 10**99
    with pytest.raises(DataError, match="100 digits"):
        convert(10**100, int_rule, Options())
    with pytest.raises(DataError, match="100 characters"):
        convert("9" * 101, num_rule, Options())
    with pytest.raises(DataError, match="Invalid decimal"):
        convert(JsonNumber("1e-101"), num_rule, Options())
    with pytest.raises(DataError, match="Invalid decimal"):
        convert(JsonNumber("not-a-number"), num_rule, Options())


def test_null_and_string_conversion_contracts() -> None:
    assert convert(True, ColumnRule(type="string"), Options()) == "true"
    assert convert(False, ColumnRule(type="string"), Options()) == "false"
    assert convert(42, ColumnRule(type="string"), Options()) == "42"
    with pytest.raises(DataError):
        convert(None, ColumnRule(type="string"), Options())


@pytest.mark.parametrize(
    ("raw", "fmt", "expected"),
    [
        ("2024-02-29", "iso", "2024-02-29"),
        ("29/02/2024", "dmy", "2024-02-29"),
        ("02/29/2024", "mdy", "2024-02-29"),
        ("02/29/2024", "auto", "2024-02-29"),
        ("12/12/2026", "auto", "2026-12-12"),
    ],
)
def test_valid_date_formats_and_leap_years(raw: str, fmt: Any, expected: str) -> None:
    assert convert(raw, ColumnRule(type="date", date_format=fmt), Options()) == expected


@pytest.mark.parametrize(
    ("raw", "fmt", "match"),
    [
        ("2026-02-29", "iso", "Invalid calendar date"),
        ("29/02/2026", "iso", "Expected an ISO date"),
        ("31/04/2026", "dmy", "Invalid calendar date"),
        ("13/01/2026", "mdy", "Invalid calendar date"),
        ("04/05/2026", "auto", "Ambiguous or invalid date"),
        (20260101, "iso", "formatted date string"),
        (True, "iso", "formatted date string"),
    ],
)
def test_invalid_or_ambiguous_dates_raise_data_error(raw: Any, fmt: Any, match: str) -> None:
    with pytest.raises(DataError, match=match):
        convert(raw, ColumnRule(type="date", date_format=fmt), Options())


def test_composite_keys_empty_keys_and_type_tagged_uniqueness() -> None:
    schema = Schema.model_validate(
        {
            "columns": {
                "region": {"type": "string", "required": True},
                "code": {"type": "string"},
            },
            "key": ["region", "code"],
        }
    )
    payload = b"region,code\nID,01\nID,\nID,01\nSG,01\n"
    result = scan(payload, "keys.csv", schema)
    assert [r["status"] for r in result["records"]] == [
        "accepted",
        "quarantine",
        "quarantine",
        "accepted",
    ]
    assert result["records"][1]["errors"][0]["code"] == "empty_key"
    assert result["records"][2]["errors"][0]["code"] == "duplicate_key"


# ---------------------------------------------------------------------------
# 4. CLI Characterization & Error Handling
# ---------------------------------------------------------------------------


def test_cli_human_summary_flags_and_error_exit_codes(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    csv_file = tmp_path / "clean.csv"
    csv_file.write_text("amount,note\n\"1,000.50\",  =ok  \n", encoding="utf-8")
    schema_file = tmp_path / "schema.json"
    schema_file.write_text(
        "\ufeff" + json.dumps({"columns": {"amount": {"type": "number"}}}),
        encoding="utf-8",
    )
    out_zip = tmp_path / "out.zip"

    code = main(
        [
            "repair",
            str(csv_file),
            "--schema",
            str(schema_file),
            "--locale",
            "en-US",
            "--keep-whitespace",
            "--allow-formulas",
            "--approve",
            "--out",
            str(out_zip),
        ]
    )
    assert code == 0
    stdout = capsys.readouterr().out
    assert "1 accepted" in stdout
    assert "Exported:" in stdout

    # Oversized input file -> exit code 2
    huge_file = tmp_path / "huge.csv"
    huge_file.write_bytes(b"x" * (MAX_BYTES + 1))
    assert main(["scan", str(huge_file)]) == 2
    assert "2 MiB" in capsys.readouterr().err

    # Non-existent input file -> exit code 2
    assert main(["scan", str(tmp_path / "missing.csv")]) == 2
    capsys.readouterr()

    # Non-UTF-8 schema file -> exit code 2 (no unhandled UnicodeDecodeError crash)
    bad_schema = tmp_path / "bad_schema.json"
    bad_schema.write_bytes(b"\xff\xfe\x00\x00")
    assert main(["scan", str(csv_file), "--schema", str(bad_schema)]) == 2
    assert "datarescue:" in capsys.readouterr().err

    # Oversized schema file -> exit code 2
    huge_schema = tmp_path / "huge_schema.json"
    huge_schema.write_bytes(b" " * (MAX_BYTES + 1))
    assert main(["scan", str(csv_file), "--schema", str(huge_schema)]) == 2
    assert "Schema file exceeds 2 MiB" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# 5. FastAPI Endpoints, Middleware, Concurrency & Handle Cleanup
# ---------------------------------------------------------------------------


def test_api_examples_health_and_home_routes(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    health_resp = client.get("/api/health")
    assert health_resp.status_code == 200
    assert health_resp.headers["x-content-type-options"] == "nosniff"
    assert health_resp.headers["cache-control"] == "no-store"

    examples_resp = client.get("/api/examples")
    assert examples_resp.status_code == 200
    example_ids = [item["id"] for item in examples_resp.json()]
    assert example_ids == ["orders", "inventory", "dates"]

    for ex_id in example_ids:
        ex_resp = client.get(f"/api/examples/{ex_id}")
        assert ex_resp.status_code == 200
        body = ex_resp.json()
        assert "filename" in body and "content" in body and "schema" in body

    # Test home() when frontend dist is absent (503) vs present (200)
    empty_dist = tmp_path / "no_dist"
    monkeypatch.setattr(api_module, "DIST", empty_dist)
    missing_home = client.get("/")
    assert missing_home.status_code == 503

    built_dist = tmp_path / "dist"
    built_dist.mkdir()
    (built_dist / "index.html").write_text("<!doctype html><title>DataRescue</title>", encoding="utf-8")
    monkeypatch.setattr(api_module, "DIST", built_dist)
    ok_home = client.get("/")
    assert ok_home.status_code == 200
    assert "DataRescue" in ok_home.text
    assert "cache-control" not in ok_home.headers or ok_home.headers["cache-control"] != "no-store"
    assert ok_home.headers["x-content-type-options"] == "nosniff"


def test_api_file_just_over_max_bytes_returns_422_and_invalid_options_returns_422() -> None:
    # Between MAX_BYTES + 1 and MAX_BYTES + 65_536 -> passes UploadLimit, caught by parse() -> 422
    over_2mb = b"a" * (MAX_BYTES + 1)
    resp = client.post("/api/scan", files={"file": ("over.csv", over_2mb, "text/csv")})
    assert resp.status_code == 422

    bad_opts = client.post(
        "/api/scan",
        files={"file": ("ok.csv", b"id\n1\n", "text/csv")},
        data={"options_json": '{"locale": "invalid"}'},
    )
    assert bad_opts.status_code == 422


def test_upload_limit_passes_non_http_scopes_through() -> None:
    called: list[str] = []

    async def dummy_app(scope: api_module.Scope, receive: api_module.Receive, send: api_module.Send) -> None:
        called.append(str(scope["type"]))

    async def dummy_receive() -> api_module.Message:
        return {"type": "lifespan.startup"}

    async def dummy_send(_message: api_module.Message) -> None:
        return None

    middleware = UploadLimit(dummy_app)
    asyncio.run(middleware({"type": "lifespan"}, dummy_receive, dummy_send))
    assert called == ["lifespan"]


def test_analyze_always_closes_upload_file_even_when_read_fails() -> None:
    closed = False

    class FailingUpload(UploadFile):
        async def read(self, size: int = -1) -> bytes:
            raise HTTPException(status_code=413, detail="Simulated stream limit")

        async def close(self) -> None:
            nonlocal closed
            closed = True
            await super().close()

    upload = FailingUpload(file=io.BytesIO(b"id\n1\n"), filename="test.csv")
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(analyze(upload, "{}", "{}"))
    assert exc_info.value.status_code == 413
    assert closed is True


def test_api_example_missing_on_disk_returns_500_cleanly(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setattr(api_module, "ROOT", tmp_path)
    resp = client.get("/api/examples/orders")
    assert resp.status_code == 500


def test_concurrent_async_scan_and_health_requests() -> None:
    async def _run_concurrent() -> tuple[httpx.Response, httpx.Response]:
        csv_rows = "id;amount\n" + "".join(f"{i:05d};1.250,50\n" for i in range(500))
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="http://test") as async_client:
            scan_task = async_client.post(
                "/api/scan",
                files={"file": ("batch.csv", csv_rows.encode(), "text/csv")},
                data={"schema_json": '{"columns":{"amount":{"type":"number"}}}'},
            )
            health_task = async_client.get("/api/health")
            return await asyncio.gather(scan_task, health_task)

    scan_resp, health_resp = asyncio.run(_run_concurrent())
    assert scan_resp.status_code == 200
    assert scan_resp.json()["summary"]["accepted"] == 500
    assert health_resp.status_code == 200

