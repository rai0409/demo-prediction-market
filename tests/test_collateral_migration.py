import sqlite3

import pytest

from app.collateral_ledger import ENGINE_KEY, POINT_SCALE, verify_collateral_invariants
from app.storage import DEMO_USER_ID, connect, get_balance, init_db


V2_TABLES = {
    "prediction_engines", "point_supply_state", "point_supply_events", "point_accounts",
    "collateral_markets", "market_reserves", "outcome_positions", "reserve_events",
    "collateral_ledger_entries", "point_allocation_events", "order_collateral_reservations",
    "order_collateral_events", "order_collateral_ledger_entries",
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


def test_order_collateral_schema_has_lookup_indexes_and_repeated_init_preserves_rows():
    conn = connect(":memory:")
    init_db(conn)
    indexes = {
        table: {row["name"] for row in conn.execute(f"pragma index_list({table})")}
        for table in ("order_collateral_reservations", "order_collateral_events", "order_collateral_ledger_entries")
    }
    assert {"idx_order_collateral_reservation_account", "idx_order_collateral_reservation_participant", "idx_order_collateral_reservation_market"} <= indexes["order_collateral_reservations"]
    assert {"idx_order_collateral_event_reservation", "idx_order_collateral_event_account"} <= indexes["order_collateral_events"]
    assert {"idx_order_collateral_ledger_reservation", "idx_order_collateral_ledger_event", "idx_order_collateral_ledger_account", "idx_order_collateral_ledger_market"} <= indexes["order_collateral_ledger_entries"]
    init_db(conn)
    assert conn.execute("pragma foreign_key_check").fetchone() is None


def _order_constraint_fixture():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("insert into point_accounts(account_id, engine_key, owner_type, owner_id, created_at, updated_at) values ('account', ?, 'participant', 'participant-1', 'x', 'x')", (ENGINE_KEY,))
    conn.execute("insert into collateral_markets(market_id, engine_key, status, point_scale, created_at, updated_at) values ('market', ?, 'open', ?, 'x', 'x')", (ENGINE_KEY, POINT_SCALE))
    conn.execute("insert into market_reserves(market_id, updated_at) values ('market', 'x')")
    return conn


def _insert_reservation(conn, **changes):
    values = {"engine_key": ENGINE_KEY, "account_id": "account", "participant_id": "participant-1", "market_id": "market", "side": "BUY", "outcome": "YES", "quantity": 1, "limit_price_micro": 1, "collateral_type": "point", "collateral_amount": 1, "status": "reserved", "release_reason": None, "version": 0, "created_at": "x", "updated_at": "x", "released_at": None} | changes
    conn.execute(
        """insert or ignore into point_accounts(
            account_id, engine_key, owner_type, owner_id, created_at, updated_at
        ) values (?, ?, 'participant', ?, 'x', 'x')""",
        (values["account_id"], ENGINE_KEY, values["participant_id"]),
    )
    columns = ", ".join(values)
    conn.execute(f"insert into order_collateral_reservations({columns}) values ({', '.join('?' for _ in values)})", tuple(values.values()))


@pytest.mark.parametrize("changes", [
    {"side": "X"}, {"outcome": "X"}, {"quantity": 0}, {"quantity": -1},
    {"limit_price_micro": 0}, {"limit_price_micro": -1}, {"limit_price_micro": 10001},
    {"collateral_type": "cash"}, {"collateral_amount": 0}, {"collateral_amount": -1},
    {"status": "X"}, {"release_reason": "X"}, {"version": -1},
    {"collateral_type": "share"}, {"collateral_amount": 2},
    {"side": "SELL", "collateral_type": "point"}, {"side": "SELL", "collateral_type": "share", "collateral_amount": 2},
    {"release_reason": "cancelled"}, {"released_at": "x"},
    {"status": "released", "release_reason": None, "released_at": "x"},
    {"status": "released", "release_reason": "cancelled", "released_at": None},
])
def test_order_collateral_reservation_constraints_reject_every_invalid_combination(changes):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_reservation(_order_constraint_fixture(), **changes)


def test_order_collateral_reservation_valid_boundaries_and_release_reasons():
    conn = _order_constraint_fixture()
    _insert_reservation(conn, limit_price_micro=1)
    _insert_reservation(conn, account_id="account-2", participant_id="participant-2", limit_price_micro=10000, collateral_amount=10000)
    _insert_reservation(conn, account_id="account-3", participant_id="participant-3", side="SELL", collateral_type="share", collateral_amount=1)
    _insert_reservation(conn, account_id="account-4", participant_id="participant-4", status="released", release_reason="cancelled", released_at="x")
    _insert_reservation(conn, account_id="account-5", participant_id="participant-5", status="released", release_reason="rejected", released_at="x")
    assert conn.execute("select count(*) from order_collateral_reservations").fetchone()[0] == 5


def _insert_event(conn, **changes):
    _insert_reservation(conn)
    values = {"engine_key": ENGINE_KEY, "reservation_id": 1, "account_id": "account", "event_type": "reserve", "release_reason": None, "asset_type": "point", "asset_amount": 1, "available_before": 1, "available_after": 0, "locked_before": 0, "locked_after": 1, "idempotency_key": "key", "request_id": None, "payload_hash": "hash", "created_at": "x"} | changes
    columns = ", ".join(values)
    conn.execute(f"insert into order_collateral_events({columns}) values ({', '.join('?' for _ in values)})", tuple(values.values()))


@pytest.mark.parametrize("changes", [
    {"event_type": "X"}, {"release_reason": "X"}, {"asset_type": "cash"}, {"asset_amount": 0}, {"asset_amount": -1},
    {"available_before": -1}, {"available_after": -1}, {"locked_before": -1}, {"locked_after": -1},
    {"available_after": 1}, {"locked_after": 0}, {"release_reason": "cancelled"},
    {"event_type": "release", "release_reason": "cancelled", "available_after": 0, "locked_before": 1, "locked_after": 0},
    {"event_type": "release", "release_reason": "cancelled", "available_before": 0, "available_after": 1, "locked_before": 1, "locked_after": 1},
    {"event_type": "release", "release_reason": None, "available_before": 0, "available_after": 1, "locked_before": 1, "locked_after": 0},
])
def test_order_collateral_event_constraints_reject_every_invalid_combination(changes):
    with pytest.raises(sqlite3.IntegrityError):
        _insert_event(_order_constraint_fixture(), **changes)


def test_order_collateral_event_valid_reserve_release_and_unique_key():
    conn = _order_constraint_fixture(); _insert_event(conn)
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute("insert into order_collateral_events(engine_key,reservation_id,account_id,event_type,asset_type,asset_amount,available_before,available_after,locked_before,locked_after,idempotency_key,payload_hash,created_at) values (?,1,'account','reserve','point',1,1,0,0,1,'key','other','x')", (ENGINE_KEY,))
    conn.execute("update order_collateral_reservations set status='released', release_reason='cancelled', released_at='x' where id=1")
    conn.execute("insert into order_collateral_events(engine_key,reservation_id,account_id,event_type,release_reason,asset_type,asset_amount,available_before,available_after,locked_before,locked_after,idempotency_key,payload_hash,created_at) values (?,1,'account','release','cancelled','point',1,0,1,1,0,'release','hash','x')", (ENGINE_KEY,))


def test_order_collateral_ledger_constraints_and_valid_balance_buckets():
    conn = _order_constraint_fixture(); _insert_event(conn)
    base = "insert into order_collateral_ledger_entries(engine_key,reservation_id,event_id,account_id,market_id,outcome,asset_type,balance_bucket,delta,balance_before,balance_after,created_at) values (?,1,1,'account','market',?,?,?,?,?,?, 'x')"
    for outcome, asset, bucket, delta, before, after in [("X","point","available",-1,1,0),("YES","cash","available",-1,1,0),("YES","point","bucket",-1,1,0),("YES","point","available",0,1,1),("YES","point","available",-1,-1,0),("YES","point","available",-1,1,-1),("YES","point","available",-1,1,1)]:
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(base, (ENGINE_KEY,outcome,asset,bucket,delta,before,after))
    conn.execute(base, (ENGINE_KEY,"YES","point","available",-1,1,0))
    conn.execute(base, (ENGINE_KEY,"YES","point","locked",1,0,1))
    with pytest.raises(sqlite3.IntegrityError):
        conn.execute(base, (ENGINE_KEY,"YES","point","available",-1,1,0))
    conn.execute("update order_collateral_reservations set status='released', release_reason='cancelled', released_at='x' where id=1")
    conn.execute("insert into order_collateral_events(engine_key,reservation_id,account_id,event_type,release_reason,asset_type,asset_amount,available_before,available_after,locked_before,locked_after,idempotency_key,payload_hash,created_at) values (?,1,'account','release','cancelled','point',1,0,1,1,0,'release','hash','x')", (ENGINE_KEY,))
    release = base.replace("event_id,account_id", "event_id,account_id").replace("?,1,1,'account'", "?,1,2,'account'")
    conn.execute(release, (ENGINE_KEY,"YES","point","locked",-1,1,0))
    conn.execute(release, (ENGINE_KEY,"YES","point","available",1,0,1))
