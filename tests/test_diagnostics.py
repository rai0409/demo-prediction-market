import sqlite3

from app.diagnostics import external_data_summary, readiness_check


def test_readiness_check_accepts_initialized_database(db_conn):
    assert readiness_check(db_conn) == {"ready": True}


def test_readiness_check_rejects_missing_tables_and_malformed_database(tmp_path):
    incomplete = sqlite3.connect(":memory:")
    incomplete.execute("pragma foreign_keys = on")
    incomplete.execute("create table markets (id text)")
    incomplete.execute("pragma user_version = 1")
    assert readiness_check(incomplete) == {"ready": False, "error_code": "schema_incomplete"}
    incomplete.close()
    malformed = sqlite3.connect(":memory:")
    malformed.close()
    assert readiness_check(malformed) == {"ready": False, "error_code": "database_unavailable"}


def test_readiness_check_rejects_legacy_schema_version(db_conn):
    db_conn.execute("pragma user_version = 0")
    assert readiness_check(db_conn) == {"ready": False, "error_code": "schema_incompatible"}


def test_external_data_summary_is_safe_and_unavailable_without_sync_history(db_conn):
    result = external_data_summary(db_conn)
    assert result == {"freshness_status": "unavailable", "last_sync_success_at": None, "last_sync_status": None}
