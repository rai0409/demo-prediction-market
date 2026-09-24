from datetime import datetime, timedelta, timezone
import json

import pytest

from app.recovery_alert import evaluate_recovery_alert


NOW = datetime(2026, 9, 23, 12, 0, tzinfo=timezone.utc)


def write_artifacts(
    tmp_path,
    *,
    health_status="PASS",
    drill_status="PASS",
    health_at=NOW,
    drill_at=NOW,
):
    health = tmp_path / "health.json"
    drill = tmp_path / "drill.json"

    health_errors = [] if health_status == "PASS" else ["safe_code"]
    health.write_text(
        json.dumps({
            "status": health_status,
            "checked_at": health_at.isoformat(),
            "backup": {
                "status": "PASS" if health_status != "FAIL" else "FAIL",
            },
            "offhost": {
                "status": health_status
                if health_status in {"PASS", "DEGRADED"}
                else "FAIL",
            },
            "error_codes": health_errors,
        }),
        encoding="utf-8",
    )

    drill.write_text(
        json.dumps({
            "drill_status": drill_status,
            "started_at": drill_at.isoformat(),
            "completed_at": drill_at.isoformat(),
            "backup": {},
            "validation": {
                "recovery_validation": (
                    "PASS" if drill_status == "PASS" else "FAIL"
                ),
                "quick_check": "ok" if drill_status == "PASS" else None,
                "foreign_key_check_rows": (
                    0 if drill_status == "PASS" else None
                ),
                "audit_chain": (
                    "verified" if drill_status == "PASS" else None
                ),
                "collateral_invariants": (
                    "verified" if drill_status == "PASS" else None
                ),
                "post_restore_health": drill_status == "PASS",
                "post_restore_ready": drill_status == "PASS",
            },
            "validator_exit_code": 0 if drill_status == "PASS" else 2,
            "error_code": (
                None if drill_status == "PASS" else "fixture_failure"
            ),
        }),
        encoding="utf-8",
    )

    return health, drill


@pytest.mark.parametrize(("health_status", "drill_status", "state", "reason"), [("PASS", "PASS", "healthy", "recovery_healthy"), ("DEGRADED", "PASS", "warning", "offhost_recovery_degraded"), ("FAIL", "PASS", "critical", "recovery_health_failed"), ("PASS", "FAIL", "critical", "restore_drill_failed")])
def test_artifact_state_mapping(tmp_path, health_status, drill_status, state, reason):
    health, drill = write_artifacts(tmp_path, health_status=health_status, drill_status=drill_status)
    result = evaluate_recovery_alert(health, drill, now=NOW)
    assert (result["state"], result["reason_code"]) == (state, reason)


def test_missing_and_stale_evidence_is_critical(tmp_path):
    health, drill = write_artifacts(tmp_path)
    assert evaluate_recovery_alert(tmp_path / "missing", drill, now=NOW)["reason_code"] == "recovery_health_missing"
    assert evaluate_recovery_alert(health, tmp_path / "missing", now=NOW)["reason_code"] == "restore_drill_missing"
    health, drill = write_artifacts(tmp_path, health_at=NOW - timedelta(hours=2, seconds=1), drill_at=NOW - timedelta(days=8, seconds=1))
    result = evaluate_recovery_alert(health, drill, now=NOW)
    assert result["state"] == "critical" and result["reason_code"] == "recovery_health_stale"
    assert result["observed_conditions"] == ["recovery_health_stale", "restore_drill_stale"]


@pytest.mark.parametrize("target", ["health", "drill"])
def test_malformed_naive_and_future_evidence_fails_closed(tmp_path, target):
    health, drill = write_artifacts(tmp_path)
    path = health if target == "health" else drill
    path.write_text("{", encoding="utf-8")
    assert evaluate_recovery_alert(health, drill, now=NOW)["state"] == "check_error"
    health, drill = write_artifacts(tmp_path)
    path = health if target == "health" else drill
    value = json.loads(path.read_text())
    value["checked_at" if target == "health" else "completed_at"] = "2026-09-23T12:00:00"
    path.write_text(json.dumps(value))
    assert evaluate_recovery_alert(health, drill, now=NOW)["state"] == "check_error"
    health, drill = write_artifacts(tmp_path)
    path = health if target == "health" else drill
    value = json.loads(path.read_text())
    value["checked_at" if target == "health" else "completed_at"] = (NOW + timedelta(seconds=301)).isoformat()
    path.write_text(json.dumps(value))
    assert evaluate_recovery_alert(health, drill, now=NOW)["state"] == "check_error"



def test_internally_inconsistent_pass_artifacts_fail_closed(tmp_path):
    health, drill = write_artifacts(tmp_path)

    value = json.loads(health.read_text(encoding="utf-8"))
    value["backup"]["status"] = "FAIL"
    health.write_text(json.dumps(value), encoding="utf-8")
    assert evaluate_recovery_alert(
        health, drill, now=NOW
    )["state"] == "check_error"

    health, drill = write_artifacts(tmp_path)
    value = json.loads(drill.read_text(encoding="utf-8"))
    value["validator_exit_code"] = 2
    drill.write_text(json.dumps(value), encoding="utf-8")
    assert evaluate_recovery_alert(
        health, drill, now=NOW
    )["state"] == "check_error"

    health, drill = write_artifacts(tmp_path)
    value = json.loads(drill.read_text(encoding="utf-8"))
    value["validation"]["post_restore_ready"] = False
    drill.write_text(json.dumps(value), encoding="utf-8")
    assert evaluate_recovery_alert(
        health, drill, now=NOW
    )["state"] == "check_error"
