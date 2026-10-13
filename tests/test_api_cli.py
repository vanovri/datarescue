import asyncio
import io
import json
import zipfile

import httpx

from datarescue.api import app
from datarescue.cli import main

class Client:
    def request(self, method, url, **kwargs):
        async def send():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test") as client:
                return await client.request(method, url, **kwargs)
        return asyncio.run(send())

    def post(self, url, **kwargs):
        return self.request("POST", url, **kwargs)

    def get(self, url, **kwargs):
        return self.request("GET", url, **kwargs)


client = Client()


def test_api_preview_export_and_changed_rules_conflict():
    source = b"id,name\n001,  Hello  \n"
    files = {"file": ("source.csv", source, "text/csv")}
    preview = client.post("/api/scan", files=files)
    assert preview.status_code == 200
    result = preview.json()
    data = {"expected_sha256": result["sha256"], "expected_plan_id": result["plan_id"]}
    response = client.post("/api/export", files=files, data=data)
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as archive:
        assert json.loads(archive.read("data.clean.json"))[0]["id"] == "001"
        assert json.loads(archive.read("report.json"))["records"][0]["original"]["name"] == "  Hello  "
    changed = {**data, "options_json": json.dumps({"trim": False})}
    assert client.post("/api/export", files=files, data=changed).status_code == 409
    assert client.post("/api/export", files={"file": ("x.csv", b"id\n2\n")}, data=data).status_code == 409


def test_bad_file_and_schema_return_readable_errors():
    assert client.post("/api/scan", files={"file": ("x.csv", b"id,id\n1,2\n")}).status_code == 422
    assert client.post("/api/scan", files={"file": ("x.csv", b"id\n1\n")}, data={"schema_json": '{"key":["missing"]}'}).status_code == 422
    assert client.get("/api/examples/not-a-file").status_code == 404


def test_api_limits_upload_body_and_disables_caching():
    response = client.post("/api/scan", files={"file": ("large.csv", b"x" * (2 * 1024 * 1024 + 70_000))})
    assert response.status_code == 413
    assert client.get("/api/health").headers["cache-control"] == "no-store"


def test_cli_never_overwrites_input_or_existing_output(tmp_path, capsys):
    source = tmp_path / "input.csv"
    original = b"id,name\n001,  Name \n"
    source.write_bytes(original)
    output = tmp_path / "result.zip"
    assert main(["repair", str(source), "--out", str(output)]) == 2
    assert not output.exists()
    assert main(["repair", str(source), "--out", str(source), "--approve"]) == 2
    assert source.read_bytes() == original
    assert main(["repair", str(source), "--out", str(output), "--approve"]) == 0
    saved = output.read_bytes()
    assert main(["repair", str(source), "--out", str(output), "--approve"]) == 2
    assert output.read_bytes() == saved
    assert source.read_bytes() == original


def test_cli_exit_one_is_a_quality_failure_not_a_crash(tmp_path, capsys):
    source = tmp_path / "input.csv"
    schema = tmp_path / "schema.json"
    source.write_text("quantity\nbroken\n", encoding="utf-8")
    schema.write_text('{"columns":{"quantity":{"type":"integer"}}}', encoding="utf-8")
    assert main(["scan", str(source), "--schema", str(schema), "--json"]) == 1
    assert json.loads(capsys.readouterr().out)["summary"]["quarantined"] == 1
