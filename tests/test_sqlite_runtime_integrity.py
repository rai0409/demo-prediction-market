import asyncio
from datetime import datetime, timedelta, timezone
import sqlite3

import pytest

from app.collateral_ledger import (
    POINT_SCALE,
    allocate_v2_points_to_participant,
    bootstrap_v2_point_supply,
    create_collateral_market,
    reserve_v2_order_collateral,
    verify_collateral_invariants,
)
from app.config import Settings
from app.diagnostics import readiness_check
from app.storage import (
    DEMO_USER_ID,
    connect,
    create_user_account,
    create_user_session,
    ensure_demo_user,
    init_db,
    resolve_user_session,
    store_markets,
)


def _create_session(conn, *, participant_id="runtime-participant"):
    ensure_demo_user(conn, participant_id)
    account = create_user_account(
        conn,
        email=f"{participant_id}@example.test",
        password="long enough password",
        participant_id=participant_id,
    )
    session, token = create_user_session(conn, user_id=account["id"], ttl_seconds=3600)
    conn.commit()
    return session, token


def _age_session(conn, session_id, current):
    previous = current - timedelta(minutes=6)
    conn.execute(
        "update user_sessions set last_seen_at = ? where id = ?",
        (previous.isoformat(), session_id),
    )
    conn.commit()
    return previous.isoformat()


def test_connect_enforces_sqlite_runtime_contract(tmp_path):
    conn = connect(str(tmp_path / "runtime.db"))
    try:
        assert conn.row_factory is sqlite3.Row
        assert conn.execute("pragma foreign_keys").fetchone()[0] == 1
        assert conn.execute("pragma busy_timeout").fetchone()[0] == 5000
    finally:
        conn.close()


def test_connect_enforces_foreign_keys_and_healthy_database_has_no_fk_violations(tmp_path):
    conn = connect(str(tmp_path / "foreign-keys.db"))
    try:
        init_db(conn)
        with pytest.raises(sqlite3.IntegrityError):
            conn.execute(
                """insert into user_sessions(id, user_id, token_hash, created_at, expires_at, last_seen_at)
                   values ('orphan', 'missing', 'hash', 'x', 'x', 'x')"""
            )
        assert conn.execute("pragma foreign_key_check").fetchall() == []
    finally:
        conn.close()


def test_request_connection_lifecycle_isolated_and_rolls_back_unfinished_work(tmp_path, monkeypatch):
    import app.main as main

    db_path = tmp_path / "request-lifecycle.db"
    monkeypatch.setattr(main, "settings", Settings(live=False, poll_seconds=30, limit=50, db_path=str(db_path)))

    async def exercise():
        first_dependency = main.get_conn()
        first = await anext(first_dependency)
        init_db(first)
        first.execute("insert into demo_users(user_id, balance) values ('uncommitted', 1)")
        await first_dependency.aclose()
        with pytest.raises(sqlite3.ProgrammingError):
            first.execute("select 1")

        second_dependency = main.get_conn()
        second = await anext(second_dependency)
        assert second is not first
        await second_dependency.aclose()

    asyncio.run(exercise())
    check = connect(str(db_path))
    try:
        assert check.execute("select count(*) from demo_users where user_id = 'uncommitted'").fetchone()[0] == 0
    finally:
        check.close()


def test_resolve_user_session_commits_standalone_touch(tmp_path):
    path = tmp_path / "standalone-touch.db"
    conn = connect(str(path))
    init_db(conn)
    session, token = _create_session(conn)
    current = datetime.now(timezone.utc).replace(microsecond=0)
    _age_session(conn, session["id"], current)

    assert resolve_user_session(conn, token, now=current)["user_id"]
    assert conn.in_transaction is False
    conn.close()

    reopened = connect(str(path))
    try:
        assert reopened.execute("select last_seen_at from user_sessions where id = ?", (session["id"],)).fetchone()[0] == current.isoformat()
    finally:
        reopened.close()


def test_resolve_user_session_preserves_caller_owned_transaction(tmp_path):
    path = tmp_path / "caller-transaction.db"
    conn = connect(str(path))
    init_db(conn)
    session, token = _create_session(conn)
    current = datetime.now(timezone.utc).replace(microsecond=0)
    original_last_seen = _age_session(conn, session["id"], current)

    conn.execute("begin")
    assert resolve_user_session(conn, token, now=current)["user_id"]
    assert conn.in_transaction is True
    conn.rollback()
    assert conn.execute("select last_seen_at from user_sessions where id = ?", (session["id"],)).fetchone()[0] == original_last_seen
    conn.close()


def test_session_touch_then_v2_reserve_persists_after_close_and_reopen(tmp_path, sample_markets):
    path = tmp_path / "session-v2-reserve.db"
    conn = connect(str(path))
    init_db(conn)
    store_markets(conn, sample_markets)
    bootstrap_v2_point_supply(conn, amount_micro=2 * POINT_SCALE, idempotency_key="runtime-bootstrap")
    allocation = allocate_v2_points_to_participant(
        conn,
        participant_id=DEMO_USER_ID,
        amount_micro=2 * POINT_SCALE,
        idempotency_key="runtime-allocation",
    )
    market_id = next(market["market_id"] for market in sample_markets if market["outcomes"] == ["YES", "NO"])
    create_collateral_market(conn, market_id=market_id)
    session, token = _create_session(conn, participant_id=DEMO_USER_ID)
    current = datetime.now(timezone.utc).replace(microsecond=0)
    _age_session(conn, session["id"], current)

    assert resolve_user_session(conn, token, now=current)["participant_id"] == DEMO_USER_ID
    reserved = reserve_v2_order_collateral(
        conn,
        participant_id=DEMO_USER_ID,
        market_id=market_id,
        side="BUY",
        outcome="YES",
        quantity=1,
        limit_price_micro=100,
        idempotency_key="runtime-reserve",
    )
    conn.close()

    reopened = connect(str(path))
    try:
        assert reopened.execute("select count(*) from order_collateral_reservations where id = ?", (reserved["reservation_id"],)).fetchone()[0] == 1
        assert reopened.execute("select locked_micro from point_accounts where account_id = ?", (allocation["destination_account_id"],)).fetchone()[0] == 100
        assert verify_collateral_invariants(reopened)["integrity_status"] == "verified"
    finally:
        reopened.close()


def test_readiness_rejects_disabled_foreign_key_enforcement(tmp_path):
    conn = connect(str(tmp_path / "foreign-keys-disabled.db"))
    try:
        init_db(conn)
        conn.execute("pragma foreign_keys = off")
        assert readiness_check(conn) == {"ready": False, "error_code": "database_unavailable"}
    finally:
        conn.close()


def test_readiness_rejects_foreign_key_violation(tmp_path):
    path = tmp_path / "foreign-key-violation.db"
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    try:
        init_db(conn)
        conn.execute(
            """insert into user_sessions(id, user_id, token_hash, created_at, expires_at, last_seen_at)
               values ('orphan', 'missing', 'hash', 'x', 'x', 'x')"""
        )
        conn.commit()
        assert readiness_check(conn) == {"ready": False, "error_code": "database_unavailable"}
    finally:
        conn.close()


def test_readiness_rejects_missing_v2_schema_after_fk_runtime_is_enabled():
    conn = sqlite3.connect(":memory:")
    try:
        conn.execute("pragma foreign_keys = on")
        conn.execute("create table markets (id text)")
        assert readiness_check(conn) == {"ready": False, "error_code": "schema_incomplete"}
    finally:
        conn.close()
