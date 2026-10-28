"""HTTP adapter for asynchronous HelixHarness telemetry supplements."""

from __future__ import annotations

import gzip
import json
from io import BytesIO

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse

from eval.stage_supplements import (
    RUNTIME_HEALTH_SCHEMAS,
    StageSupplementError,
    StageSupplementStorageError,
)

router = APIRouter()

_MAX_COMPRESSED_BYTES = 2 * 1024 * 1024
_MAX_DECOMPRESSED_BYTES = 10 * 1024 * 1024
_MAX_ITEMS = 100


@router.post("/report")
async def report(request: Request) -> JSONResponse:
    """Accept a batch without letting one permanent item error reject siblings."""

    try:
        payload = await _report_payload(request)
    except ValueError as exc:
        return JSONResponse(status_code=400, content={"detail": str(exc)})

    items = payload.get("items") if isinstance(payload, dict) else None
    if not isinstance(items, list):
        return JSONResponse(status_code=400, content={"detail": "items must be an array"})
    if len(items) > _MAX_ITEMS:
        return JSONResponse(status_code=400, content={"detail": f"items must contain at most {_MAX_ITEMS} entries"})

    acknowledgements: list[dict[str, object]] = []
    store = request.app.state.stage_supplement_store
    for item in items:
        schema = item.get("schema") if isinstance(item, dict) else None
        is_runtime_health = isinstance(schema, str) and schema in RUNTIME_HEALTH_SCHEMAS
        identifier_field = "event_id" if is_runtime_health else "supplement_id"
        identifier = item.get(identifier_field, "") if isinstance(item, dict) else ""
        try:
            if is_runtime_health:
                acknowledgements.append(store.ingest_runtime_health(item))
            else:
                acknowledgements.append(store.ingest(item))
        except StageSupplementError as exc:
            acknowledgement: dict[str, object] = {
                identifier_field: str(identifier),
                "status": "rejected",
                "error": {"code": exc.code, "message": str(exc)},
            }
            if not is_runtime_health:
                acknowledgement["linked"] = False
            acknowledgements.append(acknowledgement)
        except StageSupplementStorageError:
            acknowledgement = {
                identifier_field: str(identifier),
                "status": "retryable",
                "error": {"code": "storage_unavailable", "message": "temporary persistence failure"},
            }
            if not is_runtime_health:
                acknowledgement["linked"] = False
            acknowledgements.append(acknowledgement)
    if any(
        item.get("status") == "accepted" and item.get("linked") is True
        for item in acknowledgements
    ):
        from eval.usage_sync import schedule_usage_ai_regeneration

        schedule_usage_ai_regeneration([])
    return JSONResponse(content={"items": acknowledgements})


async def _report_payload(request: Request) -> object:
    body = await request.body()
    if len(body) > _MAX_COMPRESSED_BYTES:
        raise ValueError("request body is too large")
    content_encoding = request.headers.get("content-encoding", "").strip().lower()
    if content_encoding:
        if content_encoding != "gzip":
            raise ValueError("unsupported content encoding")
        try:
            with gzip.GzipFile(fileobj=BytesIO(body)) as compressed:
                body = compressed.read(_MAX_DECOMPRESSED_BYTES + 1)
        except (EOFError, OSError) as exc:
            raise ValueError("invalid gzip request body") from exc
    if len(body) > _MAX_DECOMPRESSED_BYTES:
        raise ValueError("decompressed request body is too large")
    try:
        return json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be valid JSON") from exc
