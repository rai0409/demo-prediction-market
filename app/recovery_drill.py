"""Read-only orchestration for scheduled isolated recovery restore drills."""

from __future__ import annotations

from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from typing import Any

from app.backup_retention import backup_inventory
from app.database_backup import _git_head


ROOT = Path(__file__).resolve().parents[1]
VALIDATOR_SCRIPT = ROOT / "scripts" / "validate_recovery.py"
DEFAULT_BACKUP_DIRECTORY = Path("runtime/backups")
DEFAULT_ARTIFACT = Path("runtime/recovery-drill/status.json")
VALIDATOR_TIMEOUT_SECONDS = 180


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
        try:
            directory_fd = os.open(path.parent, os.O_DIRECTORY)
        except OSError:
            return
        try:
            os.fsync(directory_fd)
        finally:
            os.close(directory_fd)
    finally:
        temporary.unlink(missing_ok=True)


def _validation_result(value: object) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    required = (
        "recovery_validation",
        "quick_check",
        "foreign_key_check_rows",
        "audit_chain",
        "collateral_invariants",
        "post_restore_health",
        "post_restore_ready",
        "duration_ms",
    )
    if any(key not in value for key in required):
        return None
    duration = value["duration_ms"]
    foreign_key_rows = value["foreign_key_check_rows"]
    if (
        not isinstance(value["recovery_validation"], str)
        or not isinstance(value["quick_check"], str)
        or isinstance(foreign_key_rows, bool)
        or not isinstance(foreign_key_rows, int)
        or not isinstance(value["audit_chain"], str)
        or not isinstance(value["collateral_invariants"], str)
        or not isinstance(value["post_restore_health"], bool)
        or not isinstance(value["post_restore_ready"], bool)
        or isinstance(duration, bool)
        or not isinstance(duration, int)
        or duration < 0
    ):
        return None
    error_code = value.get("error_code")
    if error_code is not None and not isinstance(error_code, str):
        return None
    return {
        "recovery_validation": value["recovery_validation"],
        "quick_check": value["quick_check"],
        "foreign_key_check_rows": foreign_key_rows,
        "audit_chain": value["audit_chain"],
        "collateral_invariants": value["collateral_invariants"],
        "post_restore_health": value["post_restore_health"],
        "post_restore_ready": value["post_restore_ready"],
        "duration_ms": duration,
        "error_code": error_code,
    }


def _valid_pass(validation: dict[str, Any]) -> bool:
    return (
        validation["recovery_validation"] == "PASS"
        and validation["quick_check"] == "ok"
        and validation["foreign_key_check_rows"] == 0
        and validation["audit_chain"] in {"verified", "empty"}
        and validation["collateral_invariants"] == "verified"
        and validation["post_restore_health"] is True
        and validation["post_restore_ready"] is True
    )


def run_scheduled_restore_drill(
    backup_directory: str | Path = DEFAULT_BACKUP_DIRECTORY,
    *,
    artifact: str | Path = DEFAULT_ARTIFACT,
    validator_timeout_seconds: int = VALIDATOR_TIMEOUT_SECONDS,
    git_head: str | None = None,
) -> dict[str, Any]:
    """Validate the newest trustworthy scheduled backup in an isolated process."""
    started_at = datetime.now(timezone.utc)
    result: dict[str, Any] = {
        "drill_status": "FAIL",
        "started_at": started_at.isoformat(),
        "completed_at": None,
        "git_head": _git_head() if git_head is None else git_head,
        "backup": {"backup_id": None, "backup_basename": None, "created_at": None},
        "validation": {
            "recovery_validation": None,
            "quick_check": None,
            "foreign_key_check_rows": None,
            "audit_chain": None,
            "collateral_invariants": None,
            "post_restore_health": None,
            "post_restore_ready": None,
            "duration_ms": None,
            "error_code": None,
        },
        "validator_exit_code": None,
        "error_code": None,
    }

    def fail(code: str) -> dict[str, Any]:
        result["error_code"] = code
        return result

    try:
        if isinstance(validator_timeout_seconds, bool) or not isinstance(validator_timeout_seconds, int) or validator_timeout_seconds <= 0:
            return fail("operational_failure")
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
        result["backup"] = {
            "backup_id": metadata["backup_id"],
            "backup_basename": backup.name,
            "created_at": metadata["created_at"],
        }
        with tempfile.TemporaryDirectory(prefix="demo-prediction-scheduled-drill-") as temporary_directory:
            validator_artifact = Path(temporary_directory) / "validator-result.json"
            command = [
                sys.executable,
                str(VALIDATOR_SCRIPT),
                "--backup",
                str(backup),
                "--artifact",
                str(validator_artifact),
                "--json",
            ]
            try:
                completed = subprocess.run(
                    command,
                    cwd=ROOT,
                    shell=False,
                    text=True,
                    capture_output=True,
                    timeout=validator_timeout_seconds,
                    check=False,
                )
            except subprocess.TimeoutExpired:
                return fail("validator_timeout")
            except OSError:
                return fail("operational_failure")
            result["validator_exit_code"] = completed.returncode

            validator_payload: object | None = None
            if validator_artifact.is_file() and not validator_artifact.is_symlink():
                try:
                    validator_payload = json.loads(validator_artifact.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    return fail("validator_artifact_invalid")
                validation = _validation_result(validator_payload)
                if validation is None:
                    return fail("validator_artifact_invalid")
                result["validation"] = validation
            elif completed.returncode == 0:
                return fail("validator_artifact_missing")

            if completed.returncode != 0:
                return fail("validator_process_failed")
            if validator_payload is None:
                return fail("validator_artifact_missing")
            if not _valid_pass(result["validation"]):
                return fail("recovery_validation_failed")
            result["drill_status"] = "PASS"
            return result
    except Exception:
        return fail("operational_failure")
    finally:
        result["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_write_json(Path(artifact), result)
