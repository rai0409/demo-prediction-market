from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any

from app.market_freshness import classify_market_freshness


WARNING_FAILURES = 3
CRITICAL_FAILURES = 6


def evaluate_sync_health(conn: sqlite3.Connection, *, now: datetime | None = None) -> dict[str, Any]:
    try:
        rows = conn.execute("select attempted_at, successful_at, status from market_sync_runs order by id desc").fetchall()
    except sqlite3.Error:
        return _error()
    if not rows:
        return _result("not_initialized", "none", "unavailable", None, None, 0, "not_initialized")
    latest = rows[0]
    successes = [row for row in rows if row[1] is not None]
    last_success = successes[0][1] if successes else None
    freshness = classify_market_freshness(last_success, now=now)
    if last_success is not None:
        try: datetime.fromisoformat(str(last_success).replace("Z", "+00:00"))
        except ValueError: return _error()
    failures = 0
    for row in rows:
        if row[1] is not None: break
        if row[2] not in {"dry_run", "sync_already_running"}: failures += 1
    if freshness["freshness_status"] == "unavailable" and last_success is not None:
        return _result("critical", "critical", "unavailable", freshness["last_sync_success_at"], latest[2], failures, "sync_unavailable")
    if failures >= CRITICAL_FAILURES:
        return _result("critical", "critical", freshness["freshness_status"], freshness["last_sync_success_at"], latest[2], failures, "repeated_sync_failures")
    if freshness["freshness_status"] == "stale":
        return _result("warning", "warning", "stale", freshness["last_sync_success_at"], latest[2], failures, "sync_stale")
    if failures >= WARNING_FAILURES:
        return _result("warning", "warning", freshness["freshness_status"], freshness["last_sync_success_at"], latest[2], failures, "repeated_sync_failures")
    return _result("healthy", "none", freshness["freshness_status"], freshness["last_sync_success_at"], latest[2], failures, "sync_healthy")


def _result(state, severity, freshness, success, status, failures, reason):
    return {"state": state, "severity": severity, "freshness_status": freshness, "last_sync_success_at": success, "last_sync_status": status, "consecutive_failures": failures, "reason_code": reason, "evaluated_at": datetime.now(timezone.utc).isoformat()}


def _error(): return _result("check_error", "critical", "unavailable", None, None, 0, "sync_check_error")


def exit_code(result: dict[str, Any]) -> int:
    return {"not_initialized": 0, "healthy": 0, "warning": 1, "critical": 2, "check_error": 3}[result["state"]]
