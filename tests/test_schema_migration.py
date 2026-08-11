from concurrent.futures import ThreadPoolExecutor

import pytest

from app.storage import (
    CURRENT_SCHEMA_REQUIRED_INDEXES,
    CURRENT_SCHEMA_REQUIRED_TABLES,
    CURRENT_SCHEMA_VERSION,
    LEGACY_SCHEMA_VERSION,
    connect,
    init_db,
)


def _version(conn):
    return conn.execute("pragma user_version").fetchone()[0]


def _table_names(conn):
    return {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}


def _index_names(conn):
    return {row[0] for row in conn.execute("select name from sqlite_master where type = 'index'")}


def test_fresh_database_migrates_from_v0_to_v1(tmp_path):
    conn = connect(str(tmp_path / "fresh.db"))
    assert _version(conn) == LEGACY_SCHEMA_VERSION
    init_db(conn)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_REQUIRED_TABLES <= _table_names(conn)
    assert CURRENT_SCHEMA_REQUIRED_INDEXES <= _index_names(conn)
    assert conn.execute("pragma foreign_key_check").fetchall() == []


def test_legacy_auth_database_migrates_without_losing_balance():
    conn = connect(":memory:")
    conn.execute("create table demo_users (user_id text primary key, balance real not null)")
    conn.execute("insert into demo_users values ('legacy-participant', 42)")
    conn.commit()
    init_db(conn)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert conn.execute("select balance from demo_users where user_id = 'legacy-participant'").fetchone()[0] == 42
    assert {"user_accounts", "user_sessions"} <= _table_names(conn)
    assert {"idx_user_sessions_user_id", "idx_user_sessions_expires_at", "idx_user_sessions_revoked_at"} <= _index_names(conn)


def test_legacy_translation_database_migrates_without_losing_rows():
    conn = connect(":memory:")
    conn.execute(
        "create table market_translations (market_id text not null, language text not null, translated_title text, "
        "translated_question text, translated_description text, source_title_hash text not null, "
        "source_question_hash text not null, source_description_hash text not null, translation_provider text not null, "
        "translation_model text not null, translation_status text not null, translated_at text not null, error_message text, "
        "primary key (market_id, language))"
    )
    conn.execute(
        "insert into market_translations values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("existing", "ja", "既存", None, None, "a", "b", "c", "fake", "v1", "success", "2026-01-01T00:00:00+00:00", None),
    )
    conn.commit()
    init_db(conn)
    init_db(conn)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert conn.execute("select translated_title from market_translations where market_id = 'existing'").fetchone()[0] == "既存"
    assert {"market_translation_attempts"} <= _table_names(conn)
    assert {
        "idx_market_translation_attempts_market_attempted",
        "idx_market_translation_attempts_quality_attempted",
        "idx_market_translation_attempts_provider_attempted",
    } <= _index_names(conn)


def test_legacy_collateral_database_migrates_without_losing_row_ids():
    conn = connect(":memory:")
    conn.execute("create table prediction_engines (engine_key text primary key, engine_version integer not null, status text not null, created_at text not null)")
    conn.execute("insert into prediction_engines values ('collateralized_clob_v2', 2, 'available', 'x')")
    conn.execute(
        "create table collateral_ledger_entries ("
        "id integer primary key autoincrement, engine_key text not null, account_id text, market_id text, "
        "entry_type text not null check(entry_type in ('bootstrap_issue', 'split_account_debit', 'split_reserve_credit', 'merge_reserve_debit', 'merge_account_credit')), "
        "amount_micro integer not null, account_available_before_micro integer, account_available_after_micro integer, "
        "reserve_before_micro integer, reserve_after_micro integer, reference_type text not null, "
        "reference_id text not null, request_id text, created_at text not null, "
        "foreign key(engine_key) references prediction_engines(engine_key))"
    )
    conn.execute(
        "insert into collateral_ledger_entries(engine_key, entry_type, amount_micro, reference_type, reference_id, created_at) "
        "values ('collateralized_clob_v2', 'bootstrap_issue', 1, 'point_supply_event', '1', 'x')"
    )
    conn.commit()
    init_db(conn)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert tuple(conn.execute("select id, entry_type, amount_micro, reference_id from collateral_ledger_entries").fetchone()) == (1, "bootstrap_issue", 1, "1")
    assert conn.execute("pragma foreign_key_check").fetchall() == []


def test_current_version_init_is_non_destructive():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("insert into demo_users values ('preserved', 7)")
    conn.commit()
    before_ledger_count = conn.execute("select count(*) from demo_point_ledger").fetchone()[0]
    init_db(conn)
    init_db(conn)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert conn.execute("select balance from demo_users where user_id = 'preserved'").fetchone()[0] == 7
    assert conn.execute("select count(*) from demo_point_ledger").fetchone()[0] == before_ledger_count


def test_failed_legacy_migration_rolls_back_all_schema_changes(tmp_path):
    path = tmp_path / "rollback.db"
    conn = connect(str(path))
    conn.execute(
        "create table collateral_ledger_entries ("
        "id integer primary key, engine_key text not null, account_id text, market_id text, entry_type text not null, "
        "amount_micro integer not null, account_available_before_micro integer, account_available_after_micro integer, "
        "reserve_before_micro integer, reserve_after_micro integer, reference_type text not null, reference_id text not null, "
        "request_id text, created_at text not null, unsupported text not null)"
    )
    conn.execute(
        "insert into collateral_ledger_entries values "
        "(1, 'legacy', null, null, 'bootstrap_issue', 1, null, null, null, null, 'legacy', '1', null, 'x', 'legacy')"
    )
    conn.commit()
    with pytest.raises(RuntimeError, match="unsupported collateral ledger schema"):
        init_db(conn)
    conn.close()

    reopened = connect(str(path))
    assert _version(reopened) == LEGACY_SCHEMA_VERSION
    assert _table_names(reopened) == {"collateral_ledger_entries"}
    assert tuple(reopened.execute("select id, unsupported from collateral_ledger_entries").fetchone()) == (1, "legacy")
    assert "collateral_ledger_entries_migrating" not in _table_names(reopened)


def test_init_db_rejects_caller_owned_transaction_without_changing_it():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("insert into demo_users values ('pending', 3)")
    with pytest.raises(RuntimeError, match="schema migration requires clean connection"):
        init_db(conn)
    assert conn.in_transaction
    assert conn.execute("select balance from demo_users where user_id = 'pending'").fetchone()[0] == 3
    conn.rollback()
    assert conn.execute("select count(*) from demo_users where user_id = 'pending'").fetchone()[0] == 0


def test_concurrent_migration_is_serialized_and_safe(tmp_path):
    path = str(tmp_path / "concurrent.db")

    def migrate():
        conn = connect(path)
        try:
            init_db(conn)
            return _version(conn)
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as executor:
        assert list(executor.map(lambda _: migrate(), range(2))) == [CURRENT_SCHEMA_VERSION, CURRENT_SCHEMA_VERSION]
    conn = connect(path)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert CURRENT_SCHEMA_REQUIRED_TABLES <= _table_names(conn)
    assert "collateral_ledger_entries_migrating" not in _table_names(conn)


def test_future_schema_version_is_rejected_without_mutation():
    conn = connect(":memory:")
    conn.execute("pragma user_version = 2")
    with pytest.raises(RuntimeError, match="unsupported schema version: 2"):
        init_db(conn)
    assert _version(conn) == 2
    assert _table_names(conn) == set()


def test_current_version_corruption_fails_closed():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("drop table markets")
    conn.commit()
    with pytest.raises(RuntimeError, match="schema v1 missing tables"):
        init_db(conn)
    assert _version(conn) == CURRENT_SCHEMA_VERSION
    assert "markets" not in _table_names(conn)
