"""Read-only, fail-closed evaluation of recovery operational artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
from typing import Any


DEFAULT_RECOVERY_HEALTH_ARTIFACT = Path("runtime/recovery-health/status.json")
DEFAULT_RESTORE_DRILL_ARTIFACT = Path("runtime/recovery-drill/status.json")
MAX_FUTURE_CLOCK_SKEW = timedelta(seconds=300)
DEFAULT_HEALTH_ARTIFACT_FRESHNESS = timedelta(hours=2)
DEFAULT_RESTORE_DRILL_MAX_AGE = timedelta(days=8)

STATES = {"healthy", "warning", "critical", "check_error"}
SEVERITIES = {"healthy": "none", "warning": "warning", "critical": "critical", "check_error": "critical"}
_CRITICAL_PRIORITY = (
    "recovery_health_failed",
    "restore_drill_failed",
    "recovery_health_stale",
    "restore_drill_stale",
    "recovery_health_missing",
    "restore_drill_missing",
)


def _utc(now: datetime | None) -> datetime:
    value = now or datetime.now(timezone.utc)
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("evaluation_time_invalid")
    return value.astimezone(timezone.utc)


def _timestamp(value: object, now: datetime) -> datetime:
    if not isinstance(value, str) or not value:
        raise ValueError("timestamp_invalid")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("timestamp_invalid") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValueError("timestamp_invalid")
    parsed = parsed.astimezone(timezone.utc)
    if parsed - now > MAX_FUTURE_CLOCK_SKEW:
        raise ValueError("timestamp_future")
    return parsed


def _read_json(path: Path) -> object:
    if path.is_symlink():
        raise ValueError("artifact_invalid")
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("artifact_invalid") from exc


def _health(value: object, now: datetime) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("artifact_invalid")
    required = {"status", "checked_at", "backup", "offhost", "error_codes"}
    if not required <= set(value) or value["status"] not in {"PASS", "DEGRADED", "FAIL"}:
        raise ValueError("artifact_invalid")
    if not isinstance(value["backup"], dict) or not isinstance(value["offhost"], dict):
        raise ValueError("artifact_invalid")
    if not isinstance(value["error_codes"], list) or not all(isinstance(code, str) for code in value["error_codes"]):
        raise ValueError("artifact_invalid")

    status = value["status"]
    error_codes = list(value["error_codes"])

    # A producer-declared PASS must also be internally consistent.
    if status == "PASS":
        if (
            value["backup"].get("status") != "PASS"
            or value["offhost"].get("status") != "PASS"
            or error_codes
        ):
            raise ValueError("artifact_invalid")

    return {
        "status": status,
        "checked_at": _timestamp(value["checked_at"], now),
        "error_codes": error_codes,
    }


def _drill(value: object, now: datetime) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise ValueError("artifact_invalid")
    required = {"drill_status", "started_at", "completed_at", "backup", "validation", "validator_exit_code", "error_code"}
    if not required <= set(value) or value["drill_status"] not in {"PASS", "FAIL"}:
        raise ValueError("artifact_invalid")
    if not isinstance(value["backup"], dict) or not isinstance(value["validation"], dict):
        raise ValueError("artifact_invalid")
    exit_code = value["validator_exit_code"]
    if (exit_code is not None and (isinstance(exit_code, bool) or not isinstance(exit_code, int))) or (
        value["error_code"] is not None and not isinstance(value["error_code"], str)
    ):
        raise ValueError("artifact_invalid")
    started_at = _timestamp(value["started_at"], now)
    completed_at = _timestamp(value["completed_at"], now)
    if completed_at < started_at:
        raise ValueError("artifact_invalid")

    if value["drill_status"] == "PASS":
        validation = value["validation"]
        foreign_key_rows = validation.get("foreign_key_check_rows")
        if (
            exit_code != 0
            or value["error_code"] is not None
            or validation.get("recovery_validation") != "PASS"
            or validation.get("quick_check") != "ok"
            or isinstance(foreign_key_rows, bool)
            or foreign_key_rows != 0
            or validation.get("audit_chain") not in {"verified", "empty"}
            or validation.get("collateral_invariants") != "verified"
            or validation.get("post_restore_health") is not True
            or validation.get("post_restore_ready") is not True
        ):
            raise ValueError("artifact_invalid")

    return {
        "status": value["drill_status"],
        "started_at": started_at,
        "completed_at": completed_at,
        "error_code": value["error_code"],
    }


def _result(state: str, reason_code: str, now: datetime, health: dict[str, Any] | None, drill: dict[str, Any] | None, conditions: list[str]) -> dict[str, Any]:
    return {
        "state": state,
        "severity": SEVERITIES[state],
        "reason_code": reason_code,
        "evaluated_at": now.isoformat(),
        "recovery_health_status": health["status"] if health else None,
        "restore_drill_status": drill["status"] if drill else None,
        "recovery_health_error_codes": health["error_codes"] if health else [],
        "restore_drill_error_code": drill["error_code"] if drill else None,
        "observed_conditions": conditions,
    }


def evaluate_recovery_alert(
    recovery_health_artifact: str | Path = DEFAULT_RECOVERY_HEALTH_ARTIFACT,
    restore_drill_artifact: str | Path = DEFAULT_RESTORE_DRILL_ARTIFACT,
    *,
    health_freshness: timedelta = DEFAULT_HEALTH_ARTIFACT_FRESHNESS,
    drill_max_age: timedelta = DEFAULT_RESTORE_DRILL_MAX_AGE,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify recovery evidence without writing artifacts or accessing the database."""
    try:
        instant = _utc(now)
    except ValueError:
        instant = datetime.now(timezone.utc)
        return _result("check_error", "evaluation_time_invalid", instant, None, None, ["evaluation_time_invalid"])
    if health_freshness < timedelta(0) or drill_max_age < timedelta(0):
        return _result("check_error", "freshness_threshold_invalid", instant, None, None, ["freshness_threshold_invalid"])

    health: dict[str, Any] | None = None
    drill: dict[str, Any] | None = None
    errors: list[str] = []
    health_path, drill_path = Path(recovery_health_artifact), Path(restore_drill_artifact)
    try:
        health_path.lstat()
    except FileNotFoundError:
        errors.append("recovery_health_missing")
    except OSError:
        errors.append("recovery_health_invalid")
    else:
        try:
            health = _health(_read_json(health_path), instant)
        except ValueError:
            errors.append("recovery_health_invalid")
    try:
        drill_path.lstat()
    except FileNotFoundError:
        errors.append("restore_drill_missing")
    except OSError:
        errors.append("restore_drill_invalid")
    else:
        try:
            drill = _drill(_read_json(drill_path), instant)
        except ValueError:
            errors.append("restore_drill_invalid")
    if any(code.endswith("_invalid") for code in errors):
        return _result("check_error", next(code for code in errors if code.endswith("_invalid")), instant, health, drill, errors)

    conditions = list(errors)
    if health is not None:
        if health["status"] == "FAIL":
            conditions.append("recovery_health_failed")
        if instant - health["checked_at"] > health_freshness:
            conditions.append("recovery_health_stale")
    if drill is not None:
        if drill["status"] == "FAIL":
            conditions.append("restore_drill_failed")
        if instant - drill["completed_at"] > drill_max_age:
            conditions.append("restore_drill_stale")
    for code in _CRITICAL_PRIORITY:
        if code in conditions:
            return _result("critical", code, instant, health, drill, conditions)
    if health is not None and drill is not None and health["status"] == "DEGRADED" and drill["status"] == "PASS":
        return _result("warning", "offhost_recovery_degraded", instant, health, drill, conditions)
    return _result("healthy", "recovery_healthy", instant, health, drill, conditions)
