from __future__ import annotations

import sqlite3
from typing import Any

from app.market_freshness import classify_market_freshness
from app.storage import get_last_successful_market_sync_run, get_latest_market_sync_run


EXPECTED_SCHEMA_VERSION = 0
REQUIRED_READINESS_TABLES = frozenset({"markets", "market_snapshots", "demo_users", "demo_point_ledger", "demo_audit_events", "market_sync_runs", "settlement_evidence", "user_accounts", "user_sessions"})


def readiness_check(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run bounded, read-only local checks; never repairs or contacts upstreams."""
    try:
        conn.execute("select 1").fetchone()
        if conn.execute("pragma quick_check").fetchone()[0] != "ok":
            return {"ready": False, "error_code": "database_unavailable"}
        version = int(conn.execute("pragma user_version").fetchone()[0])
        if version != EXPECTED_SCHEMA_VERSION:
            return {"ready": False, "error_code": "schema_incompatible"}
        tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
        if not REQUIRED_READINESS_TABLES.issubset(tables):
            return {"ready": False, "error_code": "schema_incomplete"}
    except sqlite3.Error:
        return {"ready": False, "error_code": "database_unavailable"}
    return {"ready": True}


def external_data_summary(conn: sqlite3.Connection) -> dict[str, Any]:
    last_success = get_last_successful_market_sync_run(conn) or {}
    latest = get_latest_market_sync_run(conn) or {}
    freshness = classify_market_freshness(last_success.get("successful_at"))
    return {**freshness, "last_sync_status": latest.get("status")}
