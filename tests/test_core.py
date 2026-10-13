import io
import json
import zipfile
from pathlib import Path

import pytest

from datarescue.core import DataError, Options, Schema, scan
from datarescue.export import bundle

EXAMPLES = Path(__file__).resolve().parents[1] / "examples"


def rules(columns=None, key=None, rename=None):
    return Schema.model_validate({"columns": columns or {}, "key": key or [], "rename": rename or {}})


def test_orders_end_to_end_preserves_source_identifiers_and_quarantine():
    source = (EXAMPLES / "orders.csv").read_bytes()
    schema = Schema.model_validate_json((EXAMPLES / "orders.schema.json").read_text())
    result = scan(source, "orders.csv", schema)
    assert result["summary"]["accepted"] == 7
    assert result["summary"]["quarantined"] == 5
    assert result["records"][0]["proposed"]["order_id"] == "00123"
    assert result["records"][0]["proposed"]["amount"] == "1250000.5"
    assert result["records"][0]["proposed"]["ordered_at"] == "2026-10-13"
    assert result["records"][4]["original"]["amount"] == "dua juta"
    assert (EXAMPLES / "orders.csv").read_bytes() == source
    with zipfile.ZipFile(io.BytesIO(bundle(result))) as archive:
        clean = json.loads(archive.read("data.clean.json"))
        quarantine = json.loads(archive.read("data.quarantine.json"))
        report = json.loads(archive.read("report.json"))
        assert len(clean) + len(quarantine) == result["summary"]["total"]
        assert quarantine[0]["original"]["order_id"] == "00124"
        assert len(report["export_changes"]) == 1
        assert "'=SUM(1+1)" in archive.read("data.clean.csv").decode()
        assert clean[4]["customer"] == "=SUM(1+1)"


@pytest.mark.parametrize("filename,source,expected", [
    ("values.csv", b"id;amount\n0001;1.234,50\n", "1234.5"),
    ("values.tsv", b"id\tamount\n0001\t1.234,50\n", "1234.5"),
    ("values.json", b'[{"id":"0001","amount":1.234}]', "1.234"),
    ("values.json", b'[{"id":"0001","amount":9007199254740993.25}]', "9007199254740993.25"),
    ("values.json", b'[{"id":"0001","amount":1e-3}]', "0.001"),
])
def test_locale_and_native_json_numbers_keep_exact_value(filename, source, expected):
    result = scan(source, filename, rules({"amount": {"type": "number"}}))
    assert result["records"][0]["proposed"]["amount"] == expected
    assert result["records"][0]["original"]["id"] == "0001"
    assert result["summary"]["quarantined"] == 0


def test_us_decimal_and_negative_numbers():
    result = scan(b'amount\n"-1,234.50"\n', "x.csv", rules({"amount": {"type": "number"}}), Options(locale="en-US"))
    assert result["records"][0]["proposed"]["amount"] == "-1234.5"
    with zipfile.ZipFile(io.BytesIO(bundle(result))) as archive:
        assert "'-1234.5" not in archive.read("data.clean.csv").decode()


def test_unconfigured_types_do_not_guess_identifiers_or_dates():
    result = scan(b"id,date,amount\n00123,02/03/2026,0007\n", "x.csv")
    assert result["records"][0]["proposed"] == {"id": "00123", "date": "02/03/2026", "amount": "0007"}


def test_ambiguous_dates_and_invalid_calendar_are_quarantined():
    result = scan((EXAMPLES / "dates.csv").read_bytes(), "dates.csv",
                  Schema.model_validate_json((EXAMPLES / "dates.schema.json").read_text()))
    assert result["summary"]["accepted"] == 3
    assert result["records"][0]["status"] == "quarantine"
    assert result["records"][1]["proposed"]["date"] == "2026-03-13"
    assert result["records"][3]["status"] == "quarantine"


def test_duplicate_keys_are_not_silently_dropped():
    result = scan(b"id,value\n01,a\n01,b\n", "x.csv", rules({"id": {"type": "string"}}, ["id"]))
    assert result["summary"]["total"] == 2
    assert result["records"][1]["errors"][0]["code"] == "duplicate_key"
    assert result["records"][1]["original"]["value"] == "b"


def test_invalid_first_row_does_not_reserve_its_key():
    result = scan(b"id,value\n01,word\n01,7\n", "x.csv", rules({"id": {"type": "string"}, "value": {"type": "integer"}}, ["id"]))
    assert [row["status"] for row in result["records"]] == ["quarantine", "accepted"]


def test_header_mapping_is_explicit_and_quarantine_uses_original_headers():
    schema = rules({"id": {"type": "string"}, "quantity": {"type": "integer"}}, rename={"qty": "quantity"})
    result = scan(b"id,qty\n001,4\n002,broken\n", "x.csv", schema)
    assert result["header_changes"] == [{"before": "qty", "after": "quantity"}]
    assert result["records"][0]["proposed"] == {"id": "001", "quantity": 4}
    with zipfile.ZipFile(io.BytesIO(bundle(result))) as archive:
        assert archive.read("data.clean.csv").startswith(b"id,quantity\n")
        assert archive.read("data.quarantine.csv").startswith(b"id,qty\n")


def test_header_mapping_cannot_merge_columns():
    with pytest.raises(DataError, match="merge columns"):
        scan(b"a,b\nx,y\n", "x.csv", rules(rename={"a": "b"}))


def test_missing_contract_columns_quarantine_rows_extra_fields_survive():
    result = scan(b"id,extra\n01,preserve\n", "x.csv", rules({"amount": {"type": "number"}}))
    assert result["summary"]["quarantined"] == 1
    assert result["records"][0]["original"]["extra"] == "preserve"
    assert any(item["code"] == "missing_column" for item in result["drift"])


@pytest.mark.parametrize("source,filename", [
    (b"id,id\n1,2\n", "x.csv"),
    (b"id,value\n1,2,3\n", "x.csv"),
    (b"id,value\n1\n", "x.csv"),
    (b'[{"id":1,"id":2}]', "x.json"),
    (b'[{"nested":{"value":1}}]', "x.json"),
    (b'[{"number":NaN}]', "x.json"),
    (b"id\n\xff\n", "x.csv"),
    (b"", "x.csv"),
    (b"id\n1\n", "x.xlsx"),
])
def test_lossy_or_unsupported_inputs_are_rejected(source, filename):
    with pytest.raises(DataError):
        scan(source, filename)


def test_optional_null_and_empty_fields_remain_distinct_in_report():
    result = scan(b'[{"id":"1","n":null},{"id":"2","n":""}]', "x.json", rules({"n": {"type": "number"}}))
    assert result["records"][0]["original"]["n"] is None
    assert result["records"][1]["original"]["n"] == ""
    assert result["records"][1]["proposed"]["n"] is None


def test_whitespace_is_only_changed_when_selected():
    result = scan(b"name\n  hello  \n", "x.csv", options=Options(trim=False))
    assert result["summary"]["proposed_changes"] == 0
    assert result["records"][0]["proposed"]["name"] == "  hello  "


def test_blank_csv_records_are_counted_not_confused_with_empty_fields():
    result = scan(b"id,value\n\n01,a\n02,\n\n", "x.csv")
    assert result["blank_records"] == 2
    assert result["summary"]["total"] == 2
    assert result["records"][1]["original"]["value"] == ""


def test_json_numeric_exponent_cannot_expand_without_bound():
    result = scan(b'[{"n":1e1000000}]', "x.json", rules({"n": {"type": "number"}}))
    assert result["summary"]["quarantined"] == 1


def test_preview_identity_changes_with_rules():
    source = b"id\n001\n"
    first = scan(source, "x.csv")
    second = scan(source, "x.csv", rules({"id": {"type": "integer"}}))
    assert first["sha256"] == second["sha256"]
    assert first["plan_id"] != second["plan_id"]
