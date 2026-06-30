from __future__ import annotations

from pathlib import Path
import sqlite3
from typing import Any

from app.config import Settings
from app.polymarket_gamma import (
    GAMMA_ERROR_PATH,
    GAMMA_RESPONSE_PATH,
    GAMMA_STATUS_PATH,
    fetch_live_markets,
    read_status_file,
    sample_fetch_result,
    write_status_file,
)
from app.storage import get_last_fetch_run, list_markets, replace_markets


def refresh_markets_with_result(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    if settings.live:
        live_result = fetch_live_markets(limit=settings.limit)
        if live_result.normalized_count > 0:
            replace_markets(conn, live_result.markets)
            return live_result.as_dict()

        fallback_status = (
            "live_empty_sample_fallback" if live_result.status == "live_empty" else "live_failed_sample_fallback"
        )
        sample_result = sample_fetch_result(
            limit=settings.limit,
            status=fallback_status,
            error=live_result.error or "live fetch returned no displayable markets",
            live_enabled=True,
        )
        sample_result.http_status = live_result.http_status
        sample_result.url = live_result.url
        sample_result.fallback_used = True
        write_status_file(sample_result, live_enabled=True)
        replace_markets(conn, sample_result.markets)
        return sample_result.as_dict()

    sample_result = sample_fetch_result(limit=settings.limit, status="sample_fallback", live_enabled=False)
    replace_markets(conn, sample_result.markets)
    return sample_result.as_dict()


def refresh_markets(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    return refresh_markets_with_result(conn, settings)["markets"]


def ensure_markets(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    markets = list_markets(conn)
    if not markets:
        return refresh_markets(conn, settings)
    if settings.live and all(market.get("source") == "sample" for market in markets):
        return refresh_markets(conn, settings)
    return markets


def source_status(conn: sqlite3.Connection, settings: Settings) -> dict[str, Any]:
    status_file = read_status_file() or {}
    fetch_run = get_last_fetch_run(conn) or {}
    markets = list_markets(conn)
    sample_market_count = sum(1 for market in markets if market.get("source") == "sample")
    return {
        "live_enabled": settings.live,
        "configured_limit": settings.limit,
        "configured_poll_seconds": settings.poll_seconds,
        "database_path": settings.db_path,
        "last_fetch_status": status_file.get("status") or fetch_run.get("status"),
        "last_fetch_error": status_file.get("error"),
        "last_fetch_at": status_file.get("attempted_at") or fetch_run.get("fetched_at"),
        "last_fetch_url": status_file.get("url"),
        "last_http_status": status_file.get("http_status"),
        "raw_count": status_file.get("raw_count", 0),
        "normalized_count": status_file.get("normalized_count", fetch_run.get("market_count", 0)),
        "fallback_used": bool(status_file.get("fallback_used", False)),
        "market_count": len(markets),
        "sample_market_count": sample_market_count,
        "runtime_status_file_exists": Path(GAMMA_STATUS_PATH).exists(),
        "runtime_response_file_exists": Path(GAMMA_RESPONSE_PATH).exists(),
        "runtime_error_file_exists": Path(GAMMA_ERROR_PATH).exists(),
    }
