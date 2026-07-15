from datetime import datetime, timedelta, timezone

import pytest

from app.config import Settings
from app.storage import (
    connect,
    create_user_account,
    create_user_session,
    disable_user_account,
    ensure_demo_user,
    get_user_account_by_email,
    hash_password,
    init_db,
    resolve_user_session,
    revoke_user_session,
    verify_password,
    verify_user_credentials,
)


def test_password_hash_is_salted_and_malformed_values_are_rejected():
    first = hash_password("日本語password123")
    second = hash_password("日本語password123")
    assert first != second
    assert "日本語password123" not in first
    assert verify_password("日本語password123", first)
    assert not verify_password("wrong", first)
    assert not verify_password("anything", "not-a-password-hash")


def test_auth_migration_is_additive_and_idempotent():
    conn = connect(":memory:")
    conn.execute("create table demo_users (user_id text primary key, balance real not null)")
    conn.execute("insert into demo_users values ('legacy-participant', 42)")
    init_db(conn)
    init_db(conn)
    assert conn.execute("select balance from demo_users where user_id = 'legacy-participant'").fetchone()[0] == 42
    tables = {row[0] for row in conn.execute("select name from sqlite_master where type = 'table'")}
    indexes = {row[0] for row in conn.execute("select name from sqlite_master where type = 'index'")}
    assert {"user_accounts", "user_sessions"} <= tables
    assert {"idx_user_sessions_user_id", "idx_user_sessions_expires_at", "idx_user_sessions_revoked_at"} <= indexes


def test_account_and_server_side_session_lifecycle():
    conn = connect(":memory:")
    init_db(conn)
    ensure_demo_user(conn, "auth-participant")
    account = create_user_account(conn, email="Member@Example.test", password="long enough password", participant_id="auth-participant")
    assert get_user_account_by_email(conn, "member@example.test")["id"] == account["id"]
    assert verify_user_credentials(conn, email="MEMBER@example.test", password="long enough password")
    with pytest.raises(ValueError):
        create_user_account(conn, email="member@example.test", password="long enough password", participant_id="other")
    session, raw_token = create_user_session(conn, user_id=account["id"], ttl_seconds=60)
    assert raw_token not in str(dict(conn.execute("select * from user_sessions where id = ?", (session["id"],)).fetchone()))
    assert resolve_user_session(conn, raw_token)["user_id"] == account["id"]
    assert revoke_user_session(conn, raw_token)
    assert resolve_user_session(conn, raw_token) is None


def test_expired_session_is_rejected():
    conn = connect(":memory:")
    init_db(conn)
    ensure_demo_user(conn, "expired-participant")
    account = create_user_account(conn, email="expired@example.test", password="long enough password", participant_id="expired-participant")
    session, token = create_user_session(conn, user_id=account["id"], ttl_seconds=60)
    expired = (datetime.now(timezone.utc) - timedelta(seconds=1)).isoformat()
    conn.execute("update user_sessions set expires_at = ? where id = ?", (expired, session["id"]))
    assert resolve_user_session(conn, token) is None


def test_disabling_account_revokes_existing_sessions_and_audits():
    conn = connect(":memory:")
    init_db(conn)
    ensure_demo_user(conn, "disabled-participant")
    account = create_user_account(conn, email="disabled@example.test", password="long enough password", participant_id="disabled-participant")
    _, token = create_user_session(conn, user_id=account["id"], ttl_seconds=60)
    disabled = disable_user_account(conn, account["id"])
    assert disabled["account_status"] == "disabled"
    assert resolve_user_session(conn, token) is None
    assert verify_user_credentials(conn, email="disabled@example.test", password="long enough password") is None
    assert conn.execute("select count(*) from demo_audit_events where event_type = 'account_disabled'").fetchone()[0] == 1


def test_auth_api_register_login_logout_and_identity_precedence(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "settings", Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", participant_codes="enrol-code", strict_participant_access=True))
    registered = client.post("/api/auth/register", json={"email": "member@example.test", "password": "long enough password", "participant_code": "enrol-code"}, auto_security=False)
    assert registered.status_code == 201
    assert "auth_session" in registered.headers["set-cookie"]
    assert "password" not in registered.text
    assert client.get("/api/auth/me").json()["authenticated"] is True
    # A formal authentication session wins over a conflicting demo cookie.
    client.cookies["demo_user_id"] = "other-participant"
    assert client.get("/api/demo/balance").json()["user_id"] == "enrol-code"
    assert client.post("/api/auth/logout").status_code == 200
    assert client.get("/api/auth/me").status_code == 401
    login = client.post("/api/auth/login", json={"email": "MEMBER@example.test", "password": "long enough password"}, auto_security=False)
    assert login.status_code == 200


def test_login_errors_do_not_disclose_account_existence(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "settings", Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", participant_codes="enrol-code", strict_participant_access=True))
    client.post("/api/auth/register", json={"email": "member@example.test", "password": "long enough password", "participant_code": "enrol-code"}, auto_security=False)
    known = client.post("/api/auth/login", json={"email": "member@example.test", "password": "wrong password"}, auto_security=False)
    unknown = client.post("/api/auth/login", json={"email": "unknown@example.test", "password": "wrong password"}, auto_security=False)
    assert known.status_code == unknown.status_code == 401
    assert known.json() == unknown.json() == {"detail": "invalid credentials"}


def test_login_rate_limit_uses_hashed_identity_key(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "settings", Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", auth_login_rate_limit=2, auth_login_rate_window_seconds=60))
    for _ in range(2):
        assert client.post("/api/auth/login", json={"email": "nobody@example.test", "password": "wrong password"}, auto_security=False).status_code == 401
    blocked = client.post("/api/auth/login", json={"email": "nobody@example.test", "password": "wrong password"}, auto_security=False)
    assert blocked.status_code == 429
    assert blocked.headers["retry-after"]
    assert "nobody@example.test" not in str(main._auth_failure_events)
