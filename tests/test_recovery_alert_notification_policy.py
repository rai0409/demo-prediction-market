from datetime import datetime, timedelta, timezone
import json
import subprocess
import sys

import app.recovery_alert_notification_policy as policy


NOW = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def evaluation(state, reason=None):
    return {"state": state, "severity": "none" if state == "healthy" else "critical", "reason_code": reason or state, "evaluated_at": NOW.isoformat(), "recovery_health_status": "PASS", "restore_drill_status": "PASS", "recovery_health_error_codes": [], "restore_drill_error_code": None, "observed_conditions": []}


def decide(monkeypatch, state_file, state, now=NOW):
    monkeypatch.setattr(policy, "evaluate_recovery_alert", lambda **kwargs: evaluation(state))
    return policy.decide(state_file=state_file, now=now)


def test_pending_transition_acknowledgement_and_recovery(monkeypatch, tmp_path):
    state = tmp_path / "state.json"
    warning = decide(monkeypatch, state, "warning")
    same = decide(monkeypatch, state, "warning", NOW + timedelta(minutes=1))
    assert same["decision_id"] == warning["decision_id"]
    critical = decide(monkeypatch, state, "critical", NOW + timedelta(minutes=2))
    assert critical["decision_id"] != warning["decision_id"]
    assert policy.acknowledge(critical["decision_id"], state_file=state, now=NOW + timedelta(minutes=2))["status"] == "ok"
    recovery = decide(monkeypatch, state, "healthy", NOW + timedelta(minutes=3))
    assert recovery["action"] == "notify_recovery"
    assert policy.acknowledge(recovery["decision_id"], state_file=state, now=NOW + timedelta(minutes=3))["status"] == "ok"
    assert decide(monkeypatch, state, "healthy", NOW + timedelta(minutes=4))["action"] == "none"


def test_reminders_start_after_acknowledgement(monkeypatch, tmp_path):
    for state, interval in policy.INTERVALS.items():
        path = tmp_path / f"{state}.json"
        first = decide(monkeypatch, path, state)
        assert policy.acknowledge(first["decision_id"], state_file=path, now=NOW)["status"] == "ok"
        assert decide(monkeypatch, path, state, NOW + interval - timedelta(seconds=1))["action"] == "none"
        assert decide(monkeypatch, path, state, NOW + interval)["is_reminder"] is True


def test_state_is_private_fail_closed_and_lock_is_nonblocking(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    decide(monkeypatch, path, "healthy")
    assert path.stat().st_mode & 0o777 == 0o600
    value = json.loads(path.read_text()); value["unexpected"] = True; path.write_text(json.dumps(value))
    assert decide(monkeypatch, path, "warning")["error_code"] == "state_error"
    value.pop("unexpected"); value["last_evaluated_at"] = (NOW + timedelta(seconds=1)).isoformat(); path.write_text(json.dumps(value))
    assert decide(monkeypatch, path, "warning")["error_code"] == "state_error"
    path.unlink(); decide(monkeypatch, path, "healthy"); original = path.read_bytes()
    process = subprocess.Popen([sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1],'a'); fcntl.flock(f,fcntl.LOCK_EX); print('locked',flush=True); time.sleep(5)", f"{path}.lock"], stdout=subprocess.PIPE, text=True)
    try:
        assert process.stdout and process.stdout.readline().strip() == "locked"
        assert decide(monkeypatch, path, "warning")["error_code"] == "decision_busy"
        assert path.read_bytes() == original
    finally:
        process.terminate(); process.wait(timeout=10)
