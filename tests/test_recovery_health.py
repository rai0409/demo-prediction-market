import base64
from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import uuid

import pytest

from app.backup_retention import scheduled_backup_filename
from app.database_backup import metadata_path
from app.recovery_health import check_recovery_health, evaluate_recovery_health
from scripts.check_recovery_health import main


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _backup(directory: Path, *, created_at: datetime = NOW - timedelta(hours=1), created_value: str | None = None):
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / scheduled_backup_filename(created_at)
    backup.write_bytes(b"synthetic scheduled backup")
    metadata = {
        "status": "success",
        "created_at": created_value or created_at.isoformat(),
        "backup_id": str(uuid.uuid4()),
        "backup_basename": backup.name,
        "backup_db_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    }
    metadata_path(backup).write_text(json.dumps(metadata), encoding="utf-8")
    return backup, metadata


def _state(directory: Path, backup: Path, metadata: dict, *, status: str = "PASS", now: datetime = NOW):
    payload = {
        "status": status,
        "backup_created": True,
        "backup_basename": backup.name,
        "backup_id": metadata["backup_id"],
        "backup_created_at": metadata["created_at"],
        "started_at": (now - timedelta(minutes=1)).isoformat(),
        "completed_at": now.isoformat(),
    }
    (directory / "last-run.json").write_text(json.dumps(payload), encoding="utf-8")


def _receipt(state_directory: Path, backup: Path, metadata: dict, *, verified_at: datetime = NOW):
    receipts = state_directory / "receipts"
    receipts.mkdir(parents=True, exist_ok=True)
    os.chmod(receipts, 0o700)
    sidecar = metadata_path(backup)
    sidecar_bytes = sidecar.read_bytes()
    metadata_hex = hashlib.sha256(sidecar_bytes).hexdigest()
    metadata_b64 = base64.b64encode(hashlib.sha256(sidecar_bytes).digest()).decode("ascii")
    backup_b64 = base64.b64encode(bytes.fromhex(metadata["backup_db_sha256"])).decode("ascii")
    payload = {
        "status": "VERIFIED", "provider": "s3", "backup_id": metadata["backup_id"],
        "backup_basename": backup.name, "backup_created_at": metadata["created_at"],
        "local_backup_sha256": metadata["backup_db_sha256"],
        "local_backup_size_bytes": backup.stat().st_size,
        "local_metadata_sha256": metadata_hex, "local_metadata_size_bytes": len(sidecar_bytes),
        "remote_backup_key": f"fixture/{metadata['backup_id']}/{backup.name}",
        "remote_metadata_key": f"fixture/{metadata['backup_id']}/{sidecar.name}",
        "remote_backup_checksum_sha256": backup_b64,
        "remote_metadata_checksum_sha256": metadata_b64,
        "remote_backup_size_bytes": backup.stat().st_size,
        "remote_metadata_size_bytes": len(sidecar_bytes),
        "uploaded_at": verified_at.isoformat(), "verified_at": verified_at.isoformat(),
        "backup_object_status": "VERIFIED", "metadata_object_status": "VERIFIED", "error_code": None,
    }
    path = receipts / f"{metadata['backup_id']}.json"
    path.write_text(json.dumps(payload), encoding="utf-8")
    os.chmod(path, 0o600)
    return path, payload


def _healthy_fixture(tmp_path: Path, *, now: datetime = NOW):
    backups, offhost = tmp_path / "backups", tmp_path / "offhost"
    backup, metadata = _backup(backups, created_at=now - timedelta(hours=1))
    _state(backups, backup, metadata, now=now)
    receipt, payload = _receipt(offhost, backup, metadata, verified_at=now)
    return backups, offhost, backup, metadata, receipt, payload


def _evaluate(backups, offhost):
    return evaluate_recovery_health(backups, offhost, now=NOW, git_head="test-head")


def test_recent_local_backup_with_verified_receipt_passes(tmp_path):
    backups, offhost, *_ = _healthy_fixture(tmp_path)
    result = _evaluate(backups, offhost)
    assert result["status"] == result["backup"]["status"] == result["offhost"]["status"] == "PASS"
    assert result["error_codes"] == []


def test_missing_invalid_and_wrong_receipts_degrade_healthy_local_backup(tmp_path):
    backups, offhost, backup, metadata, receipt, payload = _healthy_fixture(tmp_path)
    receipt.unlink()
    assert _evaluate(backups, offhost)["status"] == "DEGRADED"

    receipt, payload = _receipt(offhost, backup, metadata)
    payload["status"] = "FAILED"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    assert _evaluate(backups, offhost)["error_codes"] == ["receipt_status_invalid"]

    payload["status"] = "VERIFIED"
    payload["backup_id"] = str(uuid.uuid4())
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    assert _evaluate(backups, offhost)["status"] == "DEGRADED"

    payload["backup_id"] = metadata["backup_id"]
    payload["backup_basename"] = "other.sqlite3"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    assert _evaluate(backups, offhost)["error_codes"] == ["receipt_identity_mismatch"]


def test_replication_lag_above_threshold_degrades(tmp_path):
    backups, offhost, backup, metadata, *_ = _healthy_fixture(tmp_path)
    _receipt(offhost, backup, metadata, verified_at=NOW)
    result = evaluate_recovery_health(backups, offhost, now=NOW, max_replication_lag=timedelta(minutes=30))
    assert result["status"] == "DEGRADED"
    assert result["error_codes"] == ["replication_lag_exceeded"]


@pytest.mark.parametrize("created_value", ["not-a-timestamp", "2026-09-22T11:00:00"])
def test_malformed_or_naive_backup_timestamp_fails_closed(tmp_path, created_value):
    backups, offhost = tmp_path / "backups", tmp_path / "offhost"
    backup, metadata = _backup(backups, created_value=created_value)
    _state(backups, backup, metadata)
    result = _evaluate(backups, offhost)
    assert result["status"] == "FAIL"
    assert result["error_codes"] == ["backup_inventory_invalid"]


def test_stale_missing_state_failure_and_future_local_evidence_fail(tmp_path):
    backups, offhost = tmp_path / "backups", tmp_path / "offhost"
    backup, metadata = _backup(backups, created_at=NOW - timedelta(hours=31))
    _state(backups, backup, metadata)
    assert _evaluate(backups, offhost)["error_codes"] == ["backup_stale"]

    empty_directory = tmp_path / "empty"
    empty_directory.mkdir()
    empty = _evaluate(empty_directory, offhost)
    assert empty["status"] == "FAIL" and empty["error_codes"] == ["backup_missing"]

    backup, metadata = _backup(tmp_path / "malformed")
    (backup.parent / "last-run.json").write_text("{", encoding="utf-8")
    assert _evaluate(backup.parent, offhost)["error_codes"] == ["scheduled_state_invalid"]

    backup, metadata = _backup(tmp_path / "failed")
    _state(backup.parent, backup, metadata, status="FAIL")
    assert _evaluate(backup.parent, offhost)["error_codes"] == ["scheduled_state_failed"]

    backup, metadata = _backup(tmp_path / "future", created_at=NOW + timedelta(seconds=301))
    _state(backup.parent, backup, metadata)
    assert _evaluate(backup.parent, offhost)["error_codes"] == ["backup_created_at_future"]


def test_future_receipt_timestamp_degrades(tmp_path):
    backups, offhost, backup, metadata, *_ = _healthy_fixture(tmp_path)
    _receipt(offhost, backup, metadata, verified_at=NOW + timedelta(seconds=301))
    result = _evaluate(backups, offhost)
    assert result["status"] == "DEGRADED"
    assert result["error_codes"] == ["receipt_verified_at_future"]


def test_naive_or_pre_backup_receipt_timestamp_degrades(tmp_path):
    backups, offhost, backup, metadata, receipt, payload = _healthy_fixture(tmp_path)
    payload["verified_at"] = "2026-09-22T12:00:00"
    receipt.write_text(json.dumps(payload), encoding="utf-8")
    assert _evaluate(backups, offhost)["error_codes"] == ["receipt_timestamp_invalid"]

    _receipt(offhost, backup, metadata, verified_at=NOW - timedelta(hours=2))
    assert _evaluate(backups, offhost)["error_codes"] == ["receipt_verification_before_backup"]


def test_artifact_is_atomic_private_and_cli_exit_codes(tmp_path, capsys):
    runtime_now = datetime.now(timezone.utc)
    backups, offhost, *_ = _healthy_fixture(tmp_path, now=runtime_now)
    artifact = tmp_path / "artifact" / "status.json"
    result = check_recovery_health(backups, offhost, artifact=artifact, now=runtime_now, git_head="test-head")
    assert json.loads(artifact.read_text(encoding="utf-8")) == result
    assert stat.S_IMODE(artifact.stat().st_mode) == 0o600
    assert not list(artifact.parent.glob(".status.json.*"))

    assert main(["--backup-directory", str(backups), "--offhost-state-directory", str(offhost), "--artifact", str(artifact), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["status"] == "PASS"
    (offhost / "receipts" / f"{result['backup']['backup_id']}.json").unlink()
    assert main(["--backup-directory", str(backups), "--offhost-state-directory", str(offhost), "--artifact", str(artifact)]) == 2
    assert capsys.readouterr().out.strip() == "DEGRADED"
    (backups / "last-run.json").write_text("{", encoding="utf-8")
    assert main(["--backup-directory", str(backups), "--offhost-state-directory", str(offhost), "--artifact", str(artifact)]) == 2
    assert capsys.readouterr().out.strip() == "FAIL"
