"""Fail-closed orchestration for scheduled local SQLite backups."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import tempfile
from typing import Any, Iterator

from app.database_backup import BackupError, _sha256, create_backup, metadata_path


SCHEDULED_NAME = re.compile(r"scheduled-\d{8}T\d{12}Z\.sqlite3$")
LOCK_NAME = ".scheduled-backup.lock"
STATE_NAME = "last-run.json"


def scheduled_backup_filename(now: datetime | None = None) -> str:
    """Return a UTC, lexically sortable scheduler-owned filename."""
    instant = now or datetime.now(timezone.utc)
    return f"scheduled-{instant.astimezone(timezone.utc):%Y%m%dT%H%M%S%fZ}.sqlite3"


def _set_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise BackupError("permission_failed")


def _parse_created_at(value: object) -> datetime:
    if not isinstance(value, str):
        raise ValueError("created_at must be a string")
    result = datetime.fromisoformat(value)
    if result.tzinfo is None or result.utcoffset() is None:
        raise ValueError("created_at must be timezone-aware")
    return result.astimezone(timezone.utc)


def _atomic_write_json(path: Path, value: dict[str, Any]) -> None:
    descriptor, raw_temp = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw_temp)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(value, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _set_mode(temp, 0o600)
        os.replace(temp, path)
        _set_mode(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


def backup_inventory(directory: Path) -> tuple[list[tuple[datetime, Path, dict[str, Any]]], list[str]]:
    """Return eligible scheduler backups and scheduler-looking invalid artifacts.

    Invalid candidates intentionally remain in place and block destructive pruning.
    """
    eligible: list[tuple[datetime, Path, dict[str, Any]]] = []
    invalid: list[str] = []
    for backup in directory.glob("scheduled-*.sqlite3"):
        if not SCHEDULED_NAME.fullmatch(backup.name):
            invalid.append(backup.name)
            continue
        try:
            sidecar = metadata_path(backup)
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
            if not isinstance(metadata, dict):
                raise ValueError("metadata is not an object")
            created_at = _parse_created_at(metadata.get("created_at"))
            if metadata.get("status") != "success":
                raise ValueError("backup status is not success")
            if not isinstance(metadata.get("backup_id"), str) or not metadata["backup_id"]:
                raise ValueError("backup_id missing")
            if metadata.get("backup_basename") != backup.name:
                raise ValueError("backup basename mismatch")
            expected_hash = metadata.get("backup_db_sha256")
            if not isinstance(expected_hash, str) or not expected_hash:
                raise ValueError("backup hash missing")
            if expected_hash != _sha256(backup):
                raise ValueError("backup hash mismatch")
        except (OSError, ValueError, TypeError, json.JSONDecodeError):
            invalid.append(backup.name)
        else:
            eligible.append((created_at, backup, metadata))
    for sidecar in directory.glob("scheduled-*.sqlite3.metadata.json"):
        backup = sidecar.with_name(sidecar.name.removesuffix(".metadata.json"))
        if not SCHEDULED_NAME.fullmatch(backup.name) or not backup.exists():
            invalid.append(sidecar.name)
    return sorted(eligible, key=lambda candidate: candidate[0], reverse=True), sorted(invalid)


@contextmanager
def scheduled_backup_lock(directory: Path) -> Iterator[None]:
    lock_path = directory / LOCK_NAME
    with lock_path.open("a", encoding="utf-8") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BackupError("backup_run_locked") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


@contextmanager
def _secure_creation_umask() -> Iterator[None]:
    previous = os.umask(0o077)
    try:
        yield
    finally:
        os.umask(previous)


def _delete_backup_pair(backup: Path) -> None:
    backup.unlink()
    metadata_path(backup).unlink()


def _new_state(source: Path, daily_retention: int, weekly_retention: int) -> dict[str, Any]:
    return {
        "status": "FAIL",
        "started_at": datetime.now(timezone.utc).isoformat(),
        "completed_at": None,
        "git_head": None,
        "source_db_basename": source.name,
        "backup_created": False,
        "backup_basename": None,
        "metadata_basename": None,
        "backup_id": None,
        "backup_created_at": None,
        "backup_db_sha256": None,
        "quick_check": None,
        "foreign_key_check_rows": None,
        "audit_chain": None,
        "collateral_invariants": None,
        "daily_retention": daily_retention,
        "weekly_retention": weekly_retention,
        "inventory_before": [],
        "inventory_after": [],
        "kept_daily": [],
        "kept_weekly": [],
        "deleted_backups": [],
        "invalid_candidates": [],
        "retention_status": "NOT_RUN",
        "error_code": None,
    }


def run_scheduled_backup(
    source: str | Path,
    directory: str | Path,
    *,
    daily_retention: int = 7,
    weekly_retention: int = 4,
) -> dict[str, Any]:
    """Create one backup and, only with a trustworthy inventory, prune old pairs."""
    source_path = Path(source)
    backup_directory = Path(directory)
    state = _new_state(source_path, daily_retention, weekly_retention)

    try:
        if daily_retention < 0 or weekly_retention < 0:
            raise BackupError("retention_invalid")
        backup_directory.mkdir(parents=True, exist_ok=True)
        _set_mode(backup_directory, 0o700)
        with scheduled_backup_lock(backup_directory):
            before, invalid_before = backup_inventory(backup_directory)
            state["inventory_before"] = [backup.name for _, backup, _ in before]
            state["invalid_candidates"] = invalid_before

            backup_path = backup_directory / scheduled_backup_filename()
            with _secure_creation_umask():
                metadata = create_backup(source_path, backup_path, overwrite=False)
            _set_mode(backup_path, 0o600)
            _set_mode(metadata_path(backup_path), 0o600)
            state.update(
                {
                    "git_head": metadata.get("git_head"),
                    "backup_created": True,
                    "backup_basename": backup_path.name,
                    "metadata_basename": metadata_path(backup_path).name,
                    "backup_id": metadata["backup_id"],
                    "backup_created_at": metadata["created_at"],
                    "backup_db_sha256": metadata["backup_db_sha256"],
                    "quick_check": metadata["quick_check_result"],
                    "foreign_key_check_rows": metadata["foreign_key_check_rows"],
                    "audit_chain": metadata["audit_chain_result"],
                    "collateral_invariants": metadata["collateral_invariant_result"],
                }
            )

            candidates, invalid = backup_inventory(backup_directory)
            state["invalid_candidates"] = invalid
            if invalid:
                state["retention_status"] = "DEGRADED"
                raise BackupError("invalid_backup_inventory")

            daily = candidates[:daily_retention]
            keep = {backup for _, backup, _ in daily}
            weekly: list[Path] = []
            seen_weeks: set[tuple[int, int]] = set()
            for created_at, candidate, _ in candidates:
                iso_year, iso_week, _ = created_at.isocalendar()
                week = (iso_year, iso_week)
                if week not in seen_weeks and len(seen_weeks) < weekly_retention:
                    seen_weeks.add(week)
                    weekly.append(candidate)
                    keep.add(candidate)
            keep.add(backup_path)  # A current successful backup is never a prune target.
            state["kept_daily"] = [backup.name for _, backup, _ in daily]
            state["kept_weekly"] = [backup.name for backup in weekly]

            for _, candidate, _ in candidates:
                if candidate not in keep:
                    try:
                        _delete_backup_pair(candidate)
                    except OSError:
                        raise BackupError("retention_delete_failed") from None
                    state["deleted_backups"].append(candidate.name)

            after, _ = backup_inventory(backup_directory)
            state["inventory_after"] = [backup.name for _, backup, _ in after]
            state["retention_status"] = "PASS"
            state["status"] = "PASS"
    except BackupError as exc:
        state["error_code"] = exc.code
    except OSError:
        state["error_code"] = "operational_failure"
    finally:
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(backup_directory / STATE_NAME, state)
    return state
