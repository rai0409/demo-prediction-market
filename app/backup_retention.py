"""Fail-closed orchestration for scheduled local SQLite backups."""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import base64
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import uuid
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


def _receipt_result(receipts: Path | None, backup: Path, metadata: dict[str, Any]) -> tuple[str, str | None]:
    if receipts is None or not receipts.exists(): return "MISSING", "receipt_missing"
    if not receipts.is_dir() or receipts.is_symlink() or stat.S_IMODE(receipts.stat().st_mode) != 0o700: return "INVALID", "receipt_store_invalid"
    try:
        backup_id = metadata["backup_id"]
        if str(uuid.UUID(backup_id)) != backup_id: return "INVALID", "receipt_identity_mismatch"
    except (KeyError, ValueError): return "INVALID", "receipt_identity_mismatch"
    path = receipts / f"{backup_id}.json"
    if not path.exists(): return "MISSING", "receipt_missing"
    if path.is_symlink() or not path.is_file(): return "INVALID", "receipt_not_regular"
    if stat.S_IMODE(path.stat().st_mode) != 0o600: return "INVALID", "receipt_permission_invalid"
    try: receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError): return "INVALID", "receipt_json_invalid"
    if not isinstance(receipt, dict) or receipt.get("status") != "VERIFIED" or receipt.get("provider") != "s3" or receipt.get("error_code") is not None: return "INVALID", "receipt_status_invalid"
    if receipt.get("backup_id") != backup_id or receipt.get("backup_basename") != backup.name or receipt.get("backup_created_at") != metadata.get("created_at"): return "INVALID", "receipt_identity_mismatch"
    sidecar = metadata_path(backup)
    try: sidecar_bytes, size = sidecar.read_bytes(), backup.stat().st_size
    except OSError: return "INVALID", "receipt_backup_size_mismatch"
    meta_hex = hashlib.sha256(sidecar_bytes).hexdigest(); meta_b64 = base64.b64encode(hashlib.sha256(sidecar_bytes).digest()).decode("ascii")
    backup_b64 = base64.b64encode(bytes.fromhex(metadata["backup_db_sha256"])).decode("ascii")
    if receipt.get("local_backup_sha256") != metadata["backup_db_sha256"]: return "INVALID", "receipt_backup_hash_mismatch"
    if receipt.get("local_backup_size_bytes") != size or receipt.get("remote_backup_size_bytes") != size: return "INVALID", "receipt_backup_size_mismatch"
    if receipt.get("local_metadata_sha256") != meta_hex: return "INVALID", "receipt_metadata_hash_mismatch"
    if receipt.get("local_metadata_size_bytes") != len(sidecar_bytes) or receipt.get("remote_metadata_size_bytes") != len(sidecar_bytes): return "INVALID", "receipt_metadata_size_mismatch"
    if receipt.get("remote_backup_checksum_sha256") != backup_b64 or receipt.get("remote_metadata_checksum_sha256") != meta_b64: return "INVALID", "receipt_remote_checksum_mismatch"
    db_key, meta_key = receipt.get("remote_backup_key"), receipt.get("remote_metadata_key")
    if not isinstance(db_key, str) or not isinstance(meta_key, str) or not db_key.endswith(f"/{backup_id}/{backup.name}") or not meta_key.endswith(f"/{backup_id}/{sidecar.name}") or Path(db_key).parent != Path(meta_key).parent: return "INVALID", "receipt_remote_key_mismatch"
    if receipt.get("backup_object_status") not in {"VERIFIED", "ALREADY_VERIFIED"} or receipt.get("metadata_object_status") not in {"VERIFIED", "ALREADY_VERIFIED"}: return "INVALID", "receipt_object_status_invalid"
    try: uploaded, verified = _parse_created_at(receipt["uploaded_at"]), _parse_created_at(receipt["verified_at"])
    except (KeyError, ValueError): return "INVALID", "receipt_timestamp_invalid"
    return ("VERIFIED", None) if verified >= uploaded else ("INVALID", "receipt_timestamp_invalid")


def _new_state(source: Path, daily_retention: int, weekly_retention: int, receipts: Path | None) -> dict[str, Any]:
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
        "offhost_receipts_directory": receipts.name if receipts else None,
        "retention_protection_status": "NOT_RUN",
        "prune_candidates": [],
        "offhost_verified_prune_candidates": [],
        "protected_pending_offhost": [],
        "invalid_offhost_receipts": [],
        "retention_status": "NOT_RUN",
        "error_code": None,
    }


def run_scheduled_backup(
    source: str | Path,
    directory: str | Path,
    *,
    daily_retention: int = 7,
    weekly_retention: int = 4,
    offhost_receipts_directory: str | Path | None = None,
) -> dict[str, Any]:
    """Create one backup and, only with a trustworthy inventory, prune old pairs."""
    source_path = Path(source)
    backup_directory = Path(directory)
    receipts = Path(offhost_receipts_directory) if offhost_receipts_directory is not None else None
    state = _new_state(source_path, daily_retention, weekly_retention, receipts)

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

            candidate_metadata = {path: metadata for _, path, metadata in candidates}
            prune_candidates = [candidate for _, candidate, _ in candidates if candidate not in keep]
            state["prune_candidates"] = [candidate.name for candidate in prune_candidates]
            for candidate in prune_candidates:
                receipt_status, reason = _receipt_result(receipts, candidate, candidate_metadata[candidate])
                if receipt_status == "MISSING":
                    state["protected_pending_offhost"].append(candidate.name)
                    continue
                if receipt_status == "INVALID":
                    state["protected_pending_offhost"].append(candidate.name)
                    state["invalid_offhost_receipts"].append({"backup": candidate.name, "reason": reason})
                    continue
                state["offhost_verified_prune_candidates"].append(candidate.name)
                try:
                    _delete_backup_pair(candidate)
                except OSError:
                    raise BackupError("retention_delete_failed") from None
                state["deleted_backups"].append(candidate.name)

            after, _ = backup_inventory(backup_directory)
            state["inventory_after"] = [backup.name for _, backup, _ in after]
            protected = state["protected_pending_offhost"]
            state["retention_status"] = "DEGRADED" if protected else "PASS"
            state["retention_protection_status"] = "DEGRADED" if protected else "PASS"
            state["status"] = "PASS"
    except BackupError as exc:
        state["error_code"] = exc.code
    except OSError:
        state["error_code"] = "operational_failure"
    finally:
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(backup_directory / STATE_NAME, state)
    return state
