"""Stateless localhost API. Uploaded data is not retained after a request."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import Awaitable, Callable, MutableMapping
from pathlib import Path
from typing import Annotated, Any, Final, TypedDict

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import ValidationError

from .core import MAX_BYTES, DataError, Options, ScanResult, Schema, scan
from .export import bundle

logger = logging.getLogger(__name__)

Scope = MutableMapping[str, Any]
Message = MutableMapping[str, Any]
Receive = Callable[[], Awaitable[Message]]
Send = Callable[[Message], Awaitable[None]]
ASGIApp = Callable[[Scope, Receive, Send], Awaitable[None]]

METADATA_ALLOWANCE_BYTES: Final[int] = 65_536
ALLOWED_EXAMPLES: Final[dict[str, str]] = {
    "orders": "orders.csv",
    "inventory": "inventory.json",
    "dates": "dates.csv",
}

ROOT: Path = Path(__file__).resolve().parents[1]
app = FastAPI(
    title="DataRescue",
    version="0.1.0",
    description="Conservative CSV/JSON repair with a local audit.",
)


class HealthResponse(TypedDict):
    status: str
    version: str
    engine: str
    storage: str


class ExampleSummary(TypedDict):
    id: str
    name: str
    description: str


class ExamplePayload(TypedDict):
    filename: str
    content: str
    schema: dict[str, Any]


class UploadLimit:
    """Bound request bodies before multipart parsing can retain large uploads."""

    def __init__(self, app: ASGIApp) -> None:
        self.app = app

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        total = 0

        async def bounded_receive() -> Message:
            nonlocal total
            message = await receive()
            total += len(message.get("body", b""))
            if total > MAX_BYTES + METADATA_ALLOWANCE_BYTES:
                logger.warning("Rejected oversized HTTP upload body (%d bytes)", total)
                raise HTTPException(413, "Request exceeds the 2 MiB file limit plus metadata allowance.")
            return message

        await self.app(scope, bounded_receive, send)


app.add_middleware(UploadLimit)


@app.middleware("http")
async def privacy_headers(
    request: Request,
    call_next: Callable[[Request], Awaitable[Response]],
) -> Response:
    response = await call_next(request)
    if request.url.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"
    response.headers["X-Content-Type-Options"] = "nosniff"
    return response


def _run_scan(data: bytes, filename: str, schema_json: str, options_json: str) -> ScanResult:
    schema = Schema.model_validate_json(schema_json)
    options = Options.model_validate_json(options_json)
    return scan(data, filename, schema, options)


async def analyze(file: UploadFile, schema_json: str, options_json: str) -> ScanResult:
    try:
        data = await file.read(MAX_BYTES + 1)
        filename = file.filename or "input.csv"
        return await asyncio.to_thread(_run_scan, data, filename, schema_json, options_json)
    except (DataError, ValidationError) as exc:
        logger.debug("Upload analysis rejected for %r: %s", file.filename, exc)
        raise HTTPException(422, str(exc)) from exc
    finally:
        await file.close()


@app.get("/api/health")
def health() -> HealthResponse:
    return {
        "status": "ok",
        "version": "0.1.0",
        "engine": "deterministic",
        "storage": "ephemeral-upload",
    }


@app.get("/api/examples")
def examples() -> list[ExampleSummary]:
    return [
        {
            "id": "orders",
            "name": "Order export",
            "description": "IDR numbers, whitespace, invalid dates and a duplicate key.",
        },
        {
            "id": "inventory",
            "name": "JSON inventory",
            "description": "Exact JSON decimals, optional nulls and conflicting IDs.",
        },
        {
            "id": "dates",
            "name": "Ambiguous dates",
            "description": "See why guessing day and month can damage a dataset.",
        },
    ]


@app.get("/api/examples/{example_id}")
def example(example_id: str) -> ExamplePayload:
    filename = ALLOWED_EXAMPLES.get(example_id)
    if filename is None:
        raise HTTPException(404, "Example not found.")
    data_path = ROOT / "examples" / filename
    schema_path = ROOT / "examples" / f"{example_id}.schema.json"
    try:
        with data_path.open("r", encoding="utf-8") as data_handle:
            content = data_handle.read()
        with schema_path.open("r", encoding="utf-8") as schema_handle:
            schema_payload: dict[str, Any] = json.load(schema_handle)
    except (OSError, json.JSONDecodeError) as exc:
        logger.error("Failed to read bundled example %r: %s", example_id, exc)
        raise HTTPException(500, "Bundled example could not be loaded.") from exc
    return {"filename": filename, "content": content, "schema": schema_payload}


@app.post("/api/scan")
async def inspect(
    file: Annotated[UploadFile, File()],
    contract_json: Annotated[str, Form(alias="schema_json")] = "{}",
    options_json: Annotated[str, Form()] = "{}",
) -> ScanResult:
    return await analyze(file, contract_json, options_json)


@app.post("/api/export")
async def export(
    file: Annotated[UploadFile, File()],
    expected_sha256: Annotated[str, Form()],
    expected_plan_id: Annotated[str, Form()],
    contract_json: Annotated[str, Form(alias="schema_json")] = "{}",
    options_json: Annotated[str, Form()] = "{}",
) -> Response:
    result = await analyze(file, contract_json, options_json)
    if result["sha256"] != expected_sha256 or result["plan_id"] != expected_plan_id:
        logger.info("Export blocked due to stale preview digest or plan_id mismatch")
        raise HTTPException(409, "The file or rules changed after preview. Scan again before exporting.")
    archive_bytes = await asyncio.to_thread(bundle, result)
    return Response(
        archive_bytes,
        media_type="application/zip",
        headers={
            "Content-Disposition": 'attachment; filename="datarescue-results.zip"',
            "Cache-Control": "no-store",
        },
    )


DIST: Path = ROOT / "frontend" / "dist"
if (DIST / "assets").exists():
    app.mount("/assets", StaticFiles(directory=DIST / "assets"), name="assets")


@app.get("/", include_in_schema=False)
def home() -> FileResponse:
    if not (DIST / "index.html").exists():
        raise HTTPException(503, "Build the frontend with npm ci and npm run build in frontend/.")
    return FileResponse(DIST / "index.html")
