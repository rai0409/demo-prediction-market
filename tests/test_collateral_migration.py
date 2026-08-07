import sqlite3

import pytest

from app.collateral_ledger import ENGINE_KEY, POINT_SCALE, verify_collateral_invariants
from app.storage import DEMO_USER_ID, connect, get_balance, init_db


V2_TABLES = {
    "prediction_engines", "point_supply_state", "point_supply_events", "point_accounts",
    "collateral_markets", "market_reserves", "outcome_positions", "reserve_events",
    "collateral_ledger_entries", "point_allocation_events",
}


def test_empty_database_migration_creates_v2_schema_without_supply():
    conn = connect(":memory:")
    init_db(conn)
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    assert V2_TABLES <= tables
    engines = {
        row["engine_key"]: (row["engine_version"], row["status"])
        for row in conn.execute("select engine_key, engine_version, status from prediction_engines")
    }
    assert engines == {"fixed_odds_v1": (1, "legacy"), ENGINE_KEY: (2, "available")}
    assert tuple(conn.execute("select issued_micro, bootstrap_completed from point_supply_state where engine_key = ?", (ENGINE_KEY,)).fetchone()) == (0, 0)
    assert conn.execute("select count(*) from point_accounts").fetchone()[0] == 0
    assert verify_collateral_invariants(conn)["integrity_status"] == "verified"


def test_repeated_migration_is_additive_and_preserves_legacy_balance():
    conn = connect(":memory:")
    init_db(conn)
    before = get_balance(conn, DEMO_USER_ID)
    legacy_rows = conn.execute("select count(*) from demo_point_ledger").fetchone()[0]
    init_db(conn)
    assert get_balance(conn, DEMO_USER_ID) == before
    assert conn.execute("select count(*) from demo_point_ledger").fetchone()[0] == legacy_rows
    assert conn.execute("select count(*) from prediction_engines").fetchone()[0] == 2
    assert conn.execute("select count(*) from point_supply_events").fetchone()[0] == 0


def test_v2_constraints_reject_negative_values():
    conn = connect(":memory:")
    init_db(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into point_accounts(account_id, engine_key, owner_type, owner_id, available_micro, created_at, updated_at) values ('bad', ?, 'operator', 'bad', -1, 'x', 'x')", (ENGINE_KEY,))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into collateral_markets(market_id, engine_key, status, point_scale, created_at, updated_at) values ('bad', ?, 'open', ?, 'x', 'x')", (ENGINE_KEY, POINT_SCALE + 1))


def test_v2_allocation_schema_accepts_new_ledger_types_and_repeated_init_is_safe():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute(
        """insert into collateral_ledger_entries(
            engine_key, entry_type, amount_micro, reference_type, reference_id, created_at
        ) values (?, 'participant_allocation_debit', -1, 'point_allocation_event', '1', 'x')""",
        (ENGINE_KEY,),
    )
    before = tuple(conn.execute("select id, entry_type from collateral_ledger_entries").fetchone())
    init_db(conn)
    assert tuple(conn.execute("select id, entry_type from collateral_ledger_entries").fetchone()) == before
    assert conn.execute("pragma foreign_key_check").fetchone() is None


def test_legacy_collateral_ledger_schema_is_migrated_without_losing_row_ids():
    conn = connect(":memory:")
    conn.executescript(
        """
        create table prediction_engines (engine_key text primary key, engine_version integer not null, status text not null, created_at text not null);
        insert into prediction_engines values ('collateralized_clob_v2', 2, 'available', 'x');
        create table collateral_ledger_entries (
            id integer primary key autoincrement, engine_key text not null, account_id text, market_id text,
            entry_type text not null check(entry_type in ('bootstrap_issue', 'split_account_debit', 'split_reserve_credit', 'merge_reserve_debit', 'merge_account_credit')),
            amount_micro integer not null, account_available_before_micro integer, account_available_after_micro integer,
            reserve_before_micro integer, reserve_after_micro integer, reference_type text not null,
            reference_id text not null, request_id text, created_at text not null,
            foreign key(engine_key) references prediction_engines(engine_key)
        );
        insert into collateral_ledger_entries(engine_key, entry_type, amount_micro, reference_type, reference_id, created_at)
            values ('collateralized_clob_v2', 'bootstrap_issue', 1, 'point_supply_event', '1', 'x');
        """
    )
    init_db(conn)
    assert tuple(conn.execute("select id, entry_type, amount_micro from collateral_ledger_entries").fetchone()) == (1, "bootstrap_issue", 1)
    conn.execute("insert into collateral_ledger_entries(engine_key, entry_type, amount_micro, reference_type, reference_id, created_at) values (?, 'participant_allocation_credit', 1, 'point_allocation_event', '2', 'x')", (ENGINE_KEY,))
    assert conn.execute("pragma foreign_key_check").fetchone() is None
