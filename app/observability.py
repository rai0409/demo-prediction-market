from __future__ import annotations

import json
import logging
import re
import time
from typing import Any
from uuid import uuid4

from fastapi import Request


REQUEST_ID_HEADER = "X-Request-ID"
_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
logger = logging.getLogger("app.http")


def normalize_request_id(value: str | None) -> str:
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return str(uuid4())


def resolve_request_id(request: Request) -> str:
    return normalize_request_id(request.headers.get(REQUEST_ID_HEADER))


def current_request_id(request: Request) -> str:
    return request.state.request_id


def safe_log_request(*, request_id: str, method: str, path: str, status_code: int, duration_ms: float, error_type: str | None = None) -> None:
    payload: dict[str, Any] = {
        "event": "http_request",
        "request_id": request_id,
        "method": method,
        "path": path,
        "status_code": status_code,
        "duration_ms": max(0.0, round(duration_ms, 3)),
    }
    if error_type:
        payload["error_type"] = error_type
    try:
        logger.info(json.dumps(payload, separators=(",", ":"), ensure_ascii=False))
    except Exception:
        pass


def request_started_at() -> float:
    return time.perf_counter()


def elapsed_ms(started_at: float) -> float:
    return max(0.0, (time.perf_counter() - started_at) * 1000)
