from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import subprocess
import sys

import pytest

import app.sync_alert_notification_policy as policy


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def evaluation(state: str, reason: str | None = None) -> dict[str, object]:
    return {
        "state": state,
        "severity": "none" if state in {"healthy", "not_initialized"} else "warning",
        "freshness_status": "current",
        "last_sync_success_at": None,
        "last_sync_status": None,
        "consecutive_failures": 0,
        "reason_code": reason or state,
        "evaluated_at": NOW.isoformat(),
    }


def decide_state(monkeypatch, db: Path, state: str, now: datetime = NOW, reason: str | None = None):
    monkeypatch.setattr(policy, "evaluate_sync_health", lambda conn, now=None: evaluation(state, reason))
    return policy.decide(object(), str(db), now=now)


@pytest.mark.parametrize(
    ("state", "action"),
    [
        ("not_initialized", "none"),
        ("healthy", "none"),
        ("warning", "notify_warning"),
        ("critical", "notify_critical"),
        ("check_error", "notify_check_error"),
    ],
)
def test_initial_states(monkeypatch, tmp_path, state, action):
    result = decide_state(monkeypatch, tmp_path / "policy.sqlite", state)
    assert result["action"] == action
    assert result["notify"] is (action != "none")


def test_pending_decision_is_reused_replaced_and_discarded(monkeypatch, tmp_path):
    db = tmp_path / "policy.sqlite"
    warning = decide_state(monkeypatch, db, "warning", reason="first")
    original = warning["decision_id"]
    path = policy.state_path(str(db))
    before = path.read_bytes()

    same = decide_state(monkeypatch, db, "warning", NOW + timedelta(minutes=1), reason="changed_reason")
    assert same["decision_id"] == original
    assert path.read_bytes() == before

    critical = decide_state(monkeypatch, db, "critical", NOW + timedelta(minutes=2))
    assert critical["action"] == "notify_critical"
    assert critical["decision_id"] != original
    assert policy.acknowledge(str(db), original, now=NOW + timedelta(minutes=2))["error_code"] == "invalid_decision"

    healthy = decide_state(monkeypatch, db, "healthy", NOW + timedelta(minutes=3))
    assert healthy["action"] == "none"
    assert healthy["notify"] is False
    assert policy.acknowledge(str(db), critical["decision_id"], now=NOW + timedelta(minutes=3))["error_code"] == "invalid_decision"


@pytest.mark.parametrize(
    ("state", "action", "interval"),
    [
        ("warning", "notify_warning", timedelta(hours=24)),
        ("critical", "notify_critical", timedelta(hours=6)),
        ("check_error", "notify_check_error", timedelta(hours=1)),
    ],
)
def test_reminders_start_from_acknowledgement(monkeypatch, tmp_path, state, action, interval):
    db = tmp_path / f"{state}.sqlite"
    initial = decide_state(monkeypatch, db, state)
    assert policy.acknowledge(str(db), initial["decision_id"], now=NOW)["status"] == "ok"
    assert decide_state(monkeypatch, db, state, NOW + interval - timedelta(seconds=1))["action"] == "none"
    reminder = decide_state(monkeypatch, db, state, NOW + interval)
    assert reminder["action"] == action
    assert reminder["is_reminder"] is True
    assert decide_state(monkeypatch, db, state, NOW + interval + timedelta(minutes=1))["decision_id"] == reminder["decision_id"]


def test_acknowledged_abnormal_state_generates_recovery(monkeypatch, tmp_path):
    db = tmp_path / "recovery.sqlite"
    warning = decide_state(monkeypatch, db, "warning")
    assert policy.acknowledge(str(db), warning["decision_id"], now=NOW)["status"] == "ok"
    recovery = decide_state(monkeypatch, db, "healthy", NOW + timedelta(minutes=1))
    assert recovery["action"] == "notify_recovery"
    assert policy.acknowledge(str(db), recovery["decision_id"], now=NOW + timedelta(minutes=1))["status"] == "ok"
    assert decide_state(monkeypatch, db, "healthy", NOW + timedelta(minutes=2))["action"] == "none"


def test_invalid_state_is_not_rewritten(monkeypatch, tmp_path):
    db = tmp_path / "bad.sqlite"
    path = policy.state_path(str(db))
    path.write_text("[]", encoding="utf-8")
    original = path.read_bytes()
    result = decide_state(monkeypatch, db, "warning")
    assert result["error_code"] == "state_error"
    assert path.read_bytes() == original


def test_save_is_private_and_atomic(monkeypatch, tmp_path):
    db = tmp_path / "private.sqlite"
    result = decide_state(monkeypatch, db, "warning")
    path = policy.state_path(str(db))
    assert result["decision_id"]
    assert path.stat().st_mode & 0o777 == 0o600
    assert json.loads(path.read_text(encoding="utf-8"))["pending_decision_id"] == result["decision_id"]
    assert not list(path.parent.glob(f".{path.name}.*"))


def test_process_lock_is_nonblocking_and_does_not_change_state(monkeypatch, tmp_path):
    db = tmp_path / "locked.sqlite"
    path = policy.state_path(str(db))
    decide_state(monkeypatch, db, "healthy")
    original = path.read_bytes()
    script = (
        "import fcntl,sys,time; f=open(sys.argv[1], 'a'); "
        "fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(8)"
    )
    process = subprocess.Popen([sys.executable, "-c", script, f"{path}.lock"], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout is not None and process.stdout.readline().strip() == "locked"
        result = decide_state(monkeypatch, db, "warning")
        assert result["error_code"] == "decision_busy"
        assert path.read_bytes() == original
    finally:
        process.terminate()
        process.wait(timeout=10)
    assert decide_state(monkeypatch, db, "warning")["action"] == "notify_warning"


def test_state_rejects_unknown_fields_and_future_timestamp(monkeypatch, tmp_path):
    db = tmp_path / "schema.sqlite"
    decide_state(monkeypatch, db, "healthy")
    path = policy.state_path(str(db))
    value = json.loads(path.read_text(encoding="utf-8"))
    value["unexpected"] = True
    path.write_text(json.dumps(value), encoding="utf-8")
    assert decide_state(monkeypatch, db, "warning")["error_code"] == "state_error"
