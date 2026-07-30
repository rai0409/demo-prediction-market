from __future__ import annotations

from collections import Counter
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from time import monotonic
from typing import Any, Callable

from app.config import Settings
from app.polymarket_gamma import FetchResult, fetch_live_markets, utc_now_iso
from app.storage import record_market_sync_run


PROVIDER = "polymarket_public_market_data_api"
MAX_SYNC_LIMIT = 50


def _market_hash(market: dict[str, Any]) -> str:
    """Hash normalized source fields without volatile retrieval metadata."""
    stable = {key: value for key, value in market.items() if key not in {"fetched_at", "data_source_status"}}
    serialized = json.dumps(stable, ensure_ascii=False, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _safe_error_code(result: FetchResult) -> str:
    if result.status in {"timeout", "rate_limited", "upstream_4xx", "upstream_5xx", "invalid_response"}:
        return result.status
    if result.status == "live_empty":
        return "empty_response"
    return "upstream_error"


def _summary(*, requested: int, retrieved_at: str, dry_run: bool) -> dict[str, Any]:
    return {
        "requested": requested,
        "received": 0,
        "valid": 0,
        "inserted": 0,
        "updated": 0,
        "unchanged": 0,
        "skipped": 0,
        "failed": 0,
        "elapsed_ms": 0,
        "retrieved_at": retrieved_at,
        "provider": PROVIDER,
        "dry_run": dry_run,
        "error_counts": {},
        "closed": 0,
        "resolved": 0,
        "source_changed": 0,
        "source_unchanged": 0,
        "status": "pending",
        "error_code": None,
        "last_sync_success_at": None,
    }


def _finish(summary: dict[str, Any], started: float) -> dict[str, Any]:
    summary["elapsed_ms"] = int((monotonic() - started) * 1000)
    return summary


def _record_failure(conn: sqlite3.Connection, summary: dict[str, Any]) -> dict[str, Any]:
    try:
        record_market_sync_run(conn, summary)
    except sqlite3.Error:
        # The primary failure must remain safe even when diagnostic persistence is unavailable.
        summary["error_counts"] = {**summary["error_counts"], "storage_error": 1}
        summary["error_code"] = "storage_error"
        summary["status"] = "storage_error"
    return summary


def sync_polymarket_markets(
    conn: sqlite3.Connection,
    settings: Settings,
    *,
    limit: int | None = None,
    dry_run: bool = False,
    fetcher: Callable[..., FetchResult] = fetch_live_markets,
) -> dict[str, Any]:
    """Fetch once and safely upsert normalized public market-data records.

    This function intentionally does not consult or enable ``settings.live``. It
    performs a single explicit read-only request and never starts polling.
    """
    requested = min(max(1, int(limit or settings.limit)), MAX_SYNC_LIMIT)
    started = monotonic()
    summary = _summary(requested=requested, retrieved_at=utc_now_iso(), dry_run=dry_run)
    try:
        result = fetcher(limit=requested)
    except Exception:
        summary.update(status="upstream_error", error_code="upstream_error", failed=1, error_counts={"upstream_error": 1})
        _finish(summary, started)
        return _record_failure(conn, summary) if not dry_run else summary

    summary["retrieved_at"] = result.attempted_at or summary["retrieved_at"]
    summary["received"] = max(0, int(result.raw_count))
    if not result.ok or result.normalized_count <= 0:
        error_code = _safe_error_code(result) if not result.ok else "empty_response"
        summary.update(status=error_code, error_code=error_code, failed=1, error_counts={error_code: 1})
        _finish(summary, started)
        return _record_failure(conn, summary) if not dry_run else summary

    valid_markets: list[dict[str, Any]] = []
    seen_ids: set[str] = set()
    errors: Counter[str] = Counter()
    for market in result.markets:
        market_id = str(market.get("market_id") or "").strip() if isinstance(market, dict) else ""
        if not market_id:
            errors["invalid_record"] += 1
            continue
        if market_id in seen_ids:
            summary["skipped"] += 1
            errors["duplicate_market_id"] += 1
            continue
        seen_ids.add(market_id)
        valid_markets.append(market)

    summary["valid"] = len(valid_markets)
    summary["failed"] = sum(errors.values())
    summary["error_counts"] = dict(sorted(errors.items()))
    summary["closed"] = sum(1 for market in valid_markets if market.get("closed"))
    summary["resolved"] = sum(1 for market in valid_markets if market.get("resolved"))

    try:
        existing_rows = conn.execute("select market_id, payload from markets where market_id in (%s)" % ", ".join("?" for _ in valid_markets), [market["market_id"] for market in valid_markets]).fetchall() if valid_markets else []
        existing = {str(row["market_id"]): json.loads(row["payload"]) for row in existing_rows}
        plans: list[tuple[str, dict[str, Any]]] = []
        for market in valid_markets:
            old_market = existing.get(market["market_id"])
            if old_market is None:
                summary["inserted"] += 1
                summary["source_changed"] += 1
                plans.append(("insert", market))
            elif _market_hash(old_market) == _market_hash(market):
                summary["unchanged"] += 1
                summary["source_unchanged"] += 1
            else:
                summary["updated"] += 1
                summary["source_changed"] += 1
                plans.append(("update", market))
    except (sqlite3.Error, TypeError, ValueError, json.JSONDecodeError):
        summary.update(status="storage_error", error_code="storage_error", failed=summary["failed"] + 1)
        summary["error_counts"] = {**summary["error_counts"], "storage_error": 1}
        _finish(summary, started)
        return _record_failure(conn, summary) if not dry_run else summary

    if dry_run:
        summary["status"] = "dry_run"
        _finish(summary, started)
        return summary

    summary["status"] = "partial_success" if summary["failed"] else "success"
    summary["last_sync_success_at"] = summary["retrieved_at"]
    try:
        with conn:
            for operation, market in plans:
                payload = json.dumps(market, ensure_ascii=False)
                if operation == "insert":
                    conn.execute(
                        "insert into markets(market_id, payload, updated_at) values (?, ?, ?)",
                        (market["market_id"], payload, market["fetched_at"]),
                    )
                else:
                    conn.execute(
                        "update markets set payload = ?, updated_at = ? where market_id = ?",
                        (payload, market["fetched_at"], market["market_id"]),
                    )
                conn.execute(
                    "insert into market_snapshots(market_id, payload, fetched_at) values (?, ?, ?)",
                    (market["market_id"], payload, market["fetched_at"]),
                )
            record_market_sync_run(conn, summary, commit=False)
    except sqlite3.Error:
        summary.update(status="storage_error", error_code="storage_error", last_sync_success_at=None, failed=summary["failed"] + 1)
        summary["error_counts"] = {**summary["error_counts"], "storage_error": 1}
        _finish(summary, started)
        return summary

    return _finish(summary, started)
