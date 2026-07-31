from datetime import datetime, timezone
import os

from app.sync_alert_notification_policy import acknowledge, decide, state_path


def add(conn, status, success=None):
    now = datetime.now(timezone.utc).isoformat()
    conn.execute("insert into market_sync_runs(provider,attempted_at,successful_at,status,error_code,requested,received,valid,inserted,updated,unchanged,skipped,failed,error_counts_json) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", ("x", now, success, status, None, 0,0,0,0,0,0,0,0,"{}")); conn.commit()


def test_decide_acknowledge_is_atomic_and_deduplicated(db_conn, tmp_path):
    db = str(tmp_path / "policy.sqlite")
    first = decide(db_conn, db)
    assert first["action"] == "none" and first["notify"] is False
    for _ in range(3): add(db_conn, "upstream_error")
    result = decide(db_conn, db)
    assert result["action"] == "notify_warning" and result["last_notification_at"] is None
    path = state_path(db)
    assert oct(path.stat().st_mode & 0o777) == "0o600"
    assert acknowledge(db, result["decision_id"])["status"] == "ok"
    assert acknowledge(db, result["decision_id"])["error_code"] == "invalid_decision"
    assert decide(db_conn, db)["action"] == "none"
