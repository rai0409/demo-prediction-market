from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.diagnostics import readiness_check
from app.storage import (
    check_rate_limit,
    clear_rate_limit_bucket,
    connect,
    consume_rate_limit_slot,
    init_db,
)


def _consume(conn, *, limiter_type="post", action="test-action", key_hash="bucket", limit=3, window_ms=1_000, now_ms=None):
    return consume_rate_limit_slot(
        conn,
        limiter_type=limiter_type,
        action=action,
        key_hash=key_hash,
        limit=limit,
        window_ms=window_ms,
        now_ms=now_ms,
    )


def test_rate_limit_schema_is_additive_idempotent_and_required_for_readiness():
    conn = connect(":memory:")
    try:
        init_db(conn)
        demo_users_before = conn.execute("select count(*) from demo_users").fetchone()[0]
        init_db(conn)
        assert conn.execute("select count(*) from demo_users").fetchone()[0] == demo_users_before
        indexes = {row["name"] for row in conn.execute("pragma index_list(rate_limit_events)")}
        assert {"idx_rate_limit_events_bucket_expiry", "idx_rate_limit_events_expiry"} <= indexes
        assert readiness_check(conn) == {"ready": True}
        conn.execute("drop table rate_limit_events")
        assert readiness_check(conn) == {"ready": False, "error_code": "schema_incomplete"}
    finally:
        conn.close()


def test_rate_limit_post_state_survives_connection_reopen(tmp_path):
    path = tmp_path / "rate-limit-persistence.db"
    first = connect(str(path))
    init_db(first)
    for _ in range(3):
        assert _consume(first, key_hash="post-bucket") is None
    first.close()

    reopened = connect(str(path))
    try:
        assert _consume(reopened, key_hash="post-bucket") is not None
    finally:
        reopened.close()


def test_rate_limit_auth_state_survives_connection_reopen(tmp_path):
    path = tmp_path / "auth-rate-limit-persistence.db"
    first = connect(str(path))
    init_db(first)
    for _ in range(2):
        assert _consume(first, limiter_type="auth", action="login", key_hash="auth-bucket", limit=2) is None
    first.close()

    reopened = connect(str(path))
    try:
        assert _consume(reopened, limiter_type="auth", action="login", key_hash="auth-bucket", limit=2) is not None
    finally:
        reopened.close()


def test_rate_limit_multi_connection_consume_is_atomic(tmp_path):
    path = tmp_path / "rate-limit-atomic.db"
    setup = connect(str(path))
    init_db(setup)
    setup.close()

    def consume_once(_):
        conn = connect(str(path))
        try:
            return _consume(conn, key_hash="shared-bucket") is None
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=8) as pool:
        outcomes = list(pool.map(consume_once, range(8)))
    assert outcomes.count(True) == 3
    assert outcomes.count(False) == 5


def test_rate_limit_expiry_cleanup_and_clear_are_deterministic():
    conn = connect(":memory:")
    try:
        init_db(conn)
        assert _consume(conn, key_hash="expiry", limit=1, now_ms=1_000) is None
        assert check_rate_limit(conn, limiter_type="post", action="test-action", key_hash="expiry", limit=1, now_ms=1_999) == 1
        assert _consume(conn, key_hash="expiry", limit=1, now_ms=2_000) is None
        clear_rate_limit_bucket(conn, limiter_type="post", action="test-action", key_hash="expiry", now_ms=2_000)
        assert conn.execute("select count(*) from rate_limit_events").fetchone()[0] == 0
    finally:
        conn.close()


def test_rate_limit_does_not_join_or_finish_caller_transaction():
    conn = connect(":memory:")
    try:
        init_db(conn)
        conn.execute("insert into demo_users(user_id, balance) values ('uncommitted-rate-user', 1)")
        with pytest.raises(RuntimeError, match="requires clean connection"):
            _consume(conn, key_hash="outer-transaction")
        assert conn.in_transaction is True
        assert conn.execute("select count(*) from demo_users where user_id = 'uncommitted-rate-user'").fetchone()[0] == 1
        conn.rollback()
        assert conn.execute("select count(*) from demo_users where user_id = 'uncommitted-rate-user'").fetchone()[0] == 0
    finally:
        conn.close()


def test_rate_limit_rows_store_only_hashed_bucket_identity():
    conn = connect(":memory:")
    try:
        init_db(conn)
        assert _consume(conn, key_hash="a" * 64) is None
        row = conn.execute("select limiter_type, action, key_hash, occurred_at_ms, expires_at_ms from rate_limit_events").fetchone()
        assert tuple(row[:3]) == ("post", "test-action", "a" * 64)
        assert row[4] > row[3]
        serialized = str(tuple(row))
        assert not any(value in serialized for value in ("nobody@example.test", "127.0.0.1", "rate-user", "password", "token"))
    finally:
        conn.close()
