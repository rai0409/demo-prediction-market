import sqlite3

import pytest

from app.collateral_ledger import ENGINE_KEY, POINT_SCALE, verify_collateral_invariants
from app.storage import DEMO_USER_ID, connect, get_balance, init_db


V2_TABLES = {
    "prediction_engines", "point_supply_state", "point_supply_events", "point_accounts",
    "collateral_markets", "market_reserves", "outcome_positions", "reserve_events",
    "collateral_ledger_entries",
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
