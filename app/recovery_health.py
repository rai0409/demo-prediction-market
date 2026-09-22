"""Deterministic, read-only operational health evaluation for recovery evidence."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import stat
import tempfile
from typing import Any

from app.backup_retention import STATE_NAME, _parse_created_at, backup_inventory, validate_verified_receipt
from app.database_backup import _git_head


DEFAULT_BACKUP_DIRECTORY = Path("runtime/backups")
DEFAULT_OFFHOST_STATE_DIRECTORY = Path("runtime/offhost-backups")
DEFAULT_ARTIFACT = Path("runtime/recovery-health/status.json")
DEFAULT_MAX_BACKUP_AGE = timedelta(hours=30)
DEFAULT_MAX_REPLICATION_LAG = timedelta(hours=3)
CLOCK_SKEW_TOLERANCE = timedelta(seconds=300)


def _timestamp(value: object) -> datetime:
    return _parse_created_at(value)


def _seconds(value: timedelta) -> int:
    return int(value.total_seconds())


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temporary = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary, 0o600)
        os.replace(temporary, path)
        os.chmod(path, 0o600)
        directory_fd = os.open(path.parent, os.O_DIRECTORY)
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _state_matches_backup(state: object, metadata: dict[str, Any], backup: Path, now: datetime) -> str | None:
    if not isinstance(state, dict):
        return "scheduled_state_invalid"
    if state.get("status") != "PASS":
        return "scheduled_state_failed" if isinstance(state.get("status"), str) else "scheduled_state_invalid"
    if state.get("backup_created") is not True:
        return "scheduled_state_invalid"
    for key in ("started_at", "completed_at"):
        try:
            stamp = _timestamp(state.get(key))
        except (TypeError, ValueError):
            return "scheduled_state_invalid"
        if stamp - now > CLOCK_SKEW_TOLERANCE:
            return "scheduled_state_timestamp_future"
    if (
        state.get("backup_basename") != backup.name
        or state.get("backup_id") != metadata.get("backup_id")
        or state.get("backup_created_at") != metadata.get("created_at")
    ):
        return "scheduled_state_mismatch"
    return None


def _read_state(path: Path, metadata: dict[str, Any], backup: Path, now: datetime) -> str | None:
    if path.is_symlink() or not path.is_file():
        return "scheduled_state_missing"
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return "scheduled_state_invalid"
    return _state_matches_backup(state, metadata, backup, now)


def evaluate_recovery_health(
    backup_directory: str | Path = DEFAULT_BACKUP_DIRECTORY,
    offhost_state_directory: str | Path = DEFAULT_OFFHOST_STATE_DIRECTORY,
    *,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
    max_replication_lag: timedelta = DEFAULT_MAX_REPLICATION_LAG,
    now: datetime | None = None,
    git_head: str | None = None,
) -> dict[str, Any]:
    """Evaluate local and off-host recovery evidence without network activity."""
    instant = now or datetime.now(timezone.utc)
    try:
        instant = _timestamp(instant.isoformat())
    except (AttributeError, TypeError, ValueError):
        instant = datetime.now(timezone.utc)
        now_error = "health_check_time_invalid"
    else:
        now_error = None
    backup_limit = _seconds(max_backup_age)
    lag_limit = _seconds(max_replication_lag)
    result: dict[str, Any] = {
        "status": "FAIL",
        "checked_at": instant.isoformat(),
        "git_head": _git_head() if git_head is None else git_head,
        "backup": {"status": "FAIL", "backup_id": None, "backup_basename": None, "created_at": None, "age_seconds": None, "max_age_seconds": backup_limit},
        "offhost": {"status": "FAIL", "verified_at": None, "replication_lag_seconds": None, "max_replication_lag_seconds": lag_limit},
        "error_codes": [],
    }

    def fail(code: str) -> dict[str, Any]:
        result["error_codes"].append(code)
        return result

    if now_error:
        return fail(now_error)
    if backup_limit < 0:
        return fail("max_backup_age_invalid")
    if lag_limit < 0:
        return fail("max_replication_lag_invalid")

    directory = Path(backup_directory)
    try:
        if not directory.is_dir() or directory.is_symlink():
            return fail("backup_directory_missing")
        candidates, invalid = backup_inventory(directory)
    except OSError:
        return fail("backup_inventory_unavailable")
    if invalid:
        return fail("backup_inventory_invalid")
    if not candidates:
        return fail("backup_missing")

    created_at, backup, metadata = candidates[0]
    result["backup"].update({"backup_id": metadata["backup_id"], "backup_basename": backup.name, "created_at": metadata["created_at"]})
    if created_at - instant > CLOCK_SKEW_TOLERANCE:
        return fail("backup_created_at_future")
    age = max(0, _seconds(instant - created_at))
    result["backup"]["age_seconds"] = age
    if instant - created_at > max_backup_age:
        return fail("backup_stale")

    state_error = _read_state(directory / STATE_NAME, metadata, backup, instant)
    if state_error:
        return fail(state_error)
    result["backup"]["status"] = "PASS"

    receipt_status, receipt_error, receipt = validate_verified_receipt(
        Path(offhost_state_directory) / "receipts", backup, metadata
    )
    if receipt_status != "VERIFIED" or receipt is None:
        result["status"] = "DEGRADED"
        result["offhost"]["status"] = "DEGRADED"
        return fail(receipt_error or "receipt_invalid")
    try:
        verified_at = _timestamp(receipt.get("verified_at"))
    except (TypeError, ValueError):
        result["status"] = "DEGRADED"
        result["offhost"]["status"] = "DEGRADED"
        return fail("receipt_timestamp_invalid")
    result["offhost"]["verified_at"] = verified_at.isoformat()
    if verified_at - instant > CLOCK_SKEW_TOLERANCE:
        result["status"] = "DEGRADED"
        result["offhost"]["status"] = "DEGRADED"
        return fail("receipt_verified_at_future")
    if verified_at < created_at:
        result["status"] = "DEGRADED"
        result["offhost"]["status"] = "DEGRADED"
        return fail("receipt_verification_before_backup")
    lag = _seconds(verified_at - created_at)
    result["offhost"]["replication_lag_seconds"] = lag
    if verified_at - created_at > max_replication_lag:
        result["status"] = "DEGRADED"
        result["offhost"]["status"] = "DEGRADED"
        return fail("replication_lag_exceeded")
    result["status"] = "PASS"
    result["offhost"]["status"] = "PASS"
    return result


def check_recovery_health(
    backup_directory: str | Path = DEFAULT_BACKUP_DIRECTORY,
    offhost_state_directory: str | Path = DEFAULT_OFFHOST_STATE_DIRECTORY,
    *,
    artifact: str | Path = DEFAULT_ARTIFACT,
    max_backup_age: timedelta = DEFAULT_MAX_BACKUP_AGE,
    max_replication_lag: timedelta = DEFAULT_MAX_REPLICATION_LAG,
    now: datetime | None = None,
    git_head: str | None = None,
) -> dict[str, Any]:
    """Evaluate recovery evidence and atomically publish the health artifact."""
    result = evaluate_recovery_health(
        backup_directory,
        offhost_state_directory,
        max_backup_age=max_backup_age,
        max_replication_lag=max_replication_lag,
        now=now,
        git_head=git_head,
    )
    _atomic_write_json(Path(artifact), result)
    return result
