"""Stateless localhost API. Uploaded data is not retained after a request."""

import json
from pathlib import Path
from typing import Annotated

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .core import MAX_BYTES, DataError, Options, Schema, scan
from .export import bundle

ROOT = Path(__file__).resolve().parents[1]
app = FastAPI(title="DataRescue", version="0.1.0", description="Conservative CSV/JSON repair with a local audit.")


class UploadLimit:
    """Bound request bodies before multipart parsing can retain large uploads."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            return await self.app(scope, receive, send)
        total = 0

        async def bounded_receive():
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > MAX_BYTES + 65_536:
                raise HTTPException(413, "Request exceeds the 2 MiB file limit plus metadata allowance.")
            return message

        await self.app(scope, bounded_receive, send)


app.add_middleware(UploadLimit)


@app.middleware("http")
async def privacy_headers(request, call_next):
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


async def analyze(file: UploadFile, schema_json: str, options_json: str):
    data = await file.read(MAX_BYTES + 1)
    try:
        schema = Schema.model_validate_json(schema_json)
        options = Options.model_validate_json(options_json)
        return scan(data, file.filename or "input.csv", schema, options)
    except (DataError, ValidationError) as exc:
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/health")
def health():
    return {"status": "ok", "version": "0.1.0", "engine": "deterministic", "storage": "ephemeral-upload"}


@app.get("/api/examples")
def examples():
    return [
        {"id": "orders", "name": "Order export", "description": "IDR numbers, whitespace, invalid dates and a duplicate key."},
        {"id": "inventory", "name": "JSON inventory", "description": "Exact JSON decimals, optional nulls and conflicting IDs."},
        {"id": "dates", "name": "Ambiguous dates", "description": "See why guessing day and month can damage a dataset."},
    ]


@app.get("/api/examples/{example_id}")
def example(example_id: str):
    allowed = {"orders": "orders.csv", "inventory": "inventory.json", "dates": "dates.csv"}
    if example_id not in allowed:
        raise HTTPException(404, "Example not found.")
    return {"filename": allowed[example_id], "content": (ROOT / "examples" / allowed[example_id]).read_text(encoding="utf-8"),
            "schema": json.loads((ROOT / "examples" / f"{example_id}.schema.json").read_text(encoding="utf-8"))}


@app.post("/api/scan")
async def inspect(file: Annotated[UploadFile, File()], contract_json: Annotated[str, Form(alias="schema_json")] = "{}",
                  options_json: Annotated[str, Form()] = "{}"):
    return await analyze(file, contract_json, options_json)


@app.post("/api/export")
async def export(file: Annotated[UploadFile, File()], expected_sha256: Annotated[str, Form()],
                 expected_plan_id: Annotated[str, Form()],
                 contract_json: Annotated[str, Form(alias="schema_json")] = "{}", options_json: Annotated[str, Form()] = "{}"):
    result = await analyze(file, contract_json, options_json)
    if result["sha256"] != expected_sha256 or result["plan_id"] != expected_plan_id:
        raise HTTPException(409, "The file or rules changed after preview. Scan again before exporting.")
    return Response(bundle(result), media_type="application/zip",
                    headers={"Content-Disposition": 'attachment; filename="datarescue-results.zip"', "Cache-Control": "no-store"})


DIST = ROOT / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def home():
    if not (DIST / "index.html").exists():
        raise HTTPException(503, "Build the frontend with npm ci and npm run build in frontend/.")
    return FileResponse(DIST / "index.html")
