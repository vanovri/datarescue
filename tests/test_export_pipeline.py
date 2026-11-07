from __future__ import annotations

import csv
import hashlib
import io
import json
import zipfile

import pytest

from datarescue.core import ColumnRule, DataError, Options, Schema, convert, scan
from datarescue.export import bundle


@pytest.mark.parametrize(
    ("value", "expected"),
    [("TRUE", True), ("false", False), ("True", True), ("FALSE", False), (True, True), (False, False)],
)
def test_boolean_contract_accepts_only_explicit_values(value: str | bool, expected: bool) -> None:
    assert convert(value, ColumnRule(type="boolean"), Options()) is expected


@pytest.mark.parametrize("value", ["yes", "no", "1", "0", 1, 0, "aktif", "y", "n"])
def test_boolean_contract_does_not_guess_truth(value: object) -> None:
    with pytest.raises(DataError, match="true or false"):
        convert(value, ColumnRule(type="boolean"), Options())


def test_formula_headers_and_quarantine_values_survive_json_but_escape_csv() -> None:
    source = b"=name,quantity\n@formula,broken\n+safe,2\n  -leading_ws,3\n"
    schema = Schema.model_validate({"columns": {"quantity": {"type": "integer"}}})
    result = scan(source, "synthetic.csv", schema)
    assert [record["status"] for record in result["records"]] == ["quarantine", "accepted", "accepted"]

    archive_bytes = bundle(result)
    with zipfile.ZipFile(io.BytesIO(archive_bytes)) as archive:
        assert archive.testzip() is None
        assert set(archive.namelist()) == {
            "data.clean.csv",
            "data.quarantine.csv",
            "data.clean.json",
            "data.quarantine.json",
            "schema.json",
            "report.json",
        }
        clean = json.loads(archive.read("data.clean.json"))
        quarantine = json.loads(archive.read("data.quarantine.json"))
        assert clean == [{"=name": "+safe", "quantity": 2}, {"=name": "-leading_ws", "quantity": 3}]
        assert quarantine[0]["original"] == {"=name": "@formula", "quantity": "broken"}

        report = json.loads(archive.read("report.json"))
        assert report["sha256"] == hashlib.sha256(source).hexdigest()
        assert {entry["table"] for entry in report["export_changes"]} == {"clean", "quarantine"}
        assert all(entry["reason"] == "spreadsheet_formula_escape" for entry in report["export_changes"])

        quarantine_rows = list(csv.reader(io.StringIO(archive.read("data.quarantine.csv").decode("utf-8"))))
        assert quarantine_rows == [["'=name", "quantity"], ["'@formula", "broken"]]

        clean_rows = list(csv.reader(io.StringIO(archive.read("data.clean.csv").decode("utf-8"))))
        assert clean_rows == [["'=name", "quantity"], ["'+safe", "2"], ["'-leading_ws", "3"]]
        assert result["records"][0]["original"]["=name"] == "@formula"


def test_bundle_allow_formulas_disables_csv_escaping_and_preserves_booleans_and_nulls() -> None:
    source = b'[{"=cmd": "=1+1", "flag": true, "opt_num": null}]'
    schema = Schema.model_validate(
        {"columns": {"flag": {"type": "boolean"}, "opt_num": {"type": "number"}}}
    )
    result = scan(source, "items.json", schema, Options(spreadsheet_safe=False))
    with zipfile.ZipFile(io.BytesIO(bundle(result))) as archive:
        report = json.loads(archive.read("report.json"))
        assert report["export_changes"] == []
        clean_rows = list(csv.reader(io.StringIO(archive.read("data.clean.csv").decode("utf-8"))))
        assert clean_rows == [["=cmd", "flag", "opt_num"], ["=1+1", "true", ""]]
        saved_schema = json.loads(archive.read("schema.json"))
        assert saved_schema["columns"]["flag"]["type"] == "boolean"
