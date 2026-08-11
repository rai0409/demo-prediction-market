from __future__ import annotations

import sqlite3
from typing import Any

from app.market_freshness import classify_market_freshness
from app.storage import (
    CURRENT_SCHEMA_REQUIRED_TABLES,
    CURRENT_SCHEMA_VERSION,
    get_last_successful_market_sync_run,
    get_latest_market_sync_run,
)


EXPECTED_SCHEMA_VERSION = CURRENT_SCHEMA_VERSION
REQUIRED_READINESS_TABLES = CURRENT_SCHEMA_REQUIRED_TABLES


def readiness_check(conn: sqlite3.Connection) -> dict[str, Any]:
    """Run bounded, read-only local checks; never repairs or contacts upstreams."""
    try:
        conn.execute("select 1").fetchone()
        if conn.execute("pragma quick_check").fetchone()[0] != "ok":
            return {"ready": False, "error_code": "database_unavailable"}
        if int(conn.execute("pragma foreign_keys").fetchone()[0]) != 1:
            return {"ready": False, "error_code": "database_unavailable"}
        if conn.execute("pragma foreign_key_check").fetchone() is not None:
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
