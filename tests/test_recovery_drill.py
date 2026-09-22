from datetime import datetime, timedelta, timezone
import hashlib
import json
import os
from pathlib import Path
import stat
import subprocess
import uuid

import app.recovery_drill as recovery_drill
from app.backup_retention import scheduled_backup_filename
from app.database_backup import create_backup, metadata_path
from scripts import run_scheduled_restore_drill as drill_cli


NOW = datetime(2026, 9, 22, 12, 0, tzinfo=timezone.utc)


def _scheduled_backup(directory: Path, instant: datetime):
    directory.mkdir(parents=True, exist_ok=True)
    backup = directory / scheduled_backup_filename(instant)
    backup.write_bytes(f"scheduled backup {instant.isoformat()}".encode("ascii"))
    metadata = {
        "status": "success",
        "created_at": instant.isoformat(),
        "backup_id": str(uuid.uuid4()),
        "backup_basename": backup.name,
        "backup_db_sha256": hashlib.sha256(backup.read_bytes()).hexdigest(),
    }
    metadata_path(backup).write_text(json.dumps(metadata), encoding="utf-8")
    return backup, metadata


def _validator_pass(**overrides):
    result = {
        "recovery_validation": "PASS",
        "quick_check": "ok",
        "foreign_key_check_rows": 0,
        "audit_chain": "verified",
        "collateral_invariants": "verified",
        "post_restore_health": True,
        "post_restore_ready": True,
        "duration_ms": 12,
        "error_code": None,
    }
    result.update(overrides)
    return result


def _run_with_validator(monkeypatch, payload: object | None = None, *, returncode: int = 0, timeout: bool = False):
    calls: list[tuple[list[str], dict]] = []

    def fake_run(command, **kwargs):
        calls.append((command, kwargs))
        if timeout:
            raise subprocess.TimeoutExpired(command, kwargs["timeout"])
        artifact = Path(command[command.index("--artifact") + 1])
        if payload is not None:
            if isinstance(payload, str):
                artifact.write_text(payload, encoding="utf-8")
            else:
                artifact.write_text(json.dumps(payload), encoding="utf-8")
        return subprocess.CompletedProcess(command, returncode)

    monkeypatch.setattr(recovery_drill.subprocess, "run", fake_run)
    return calls


def test_newest_valid_scheduled_backup_is_selected_and_validator_contract_is_safe(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    older, _ = _scheduled_backup(backups, NOW - timedelta(hours=2))
    newest, metadata = _scheduled_backup(backups, NOW - timedelta(hours=1))
    calls = _run_with_validator(monkeypatch, _validator_pass())

    result = recovery_drill.run_scheduled_restore_drill(backups, artifact=tmp_path / "status.json", git_head="test-head")

    assert result["drill_status"] == "PASS"
    assert result["backup"] == {"backup_id": metadata["backup_id"], "backup_basename": newest.name, "created_at": metadata["created_at"]}
    command, kwargs = calls[0]
    assert command == [
        recovery_drill.sys.executable, str(recovery_drill.VALIDATOR_SCRIPT), "--backup", str(newest),
        "--artifact", command[5], "--json",
    ]
    assert kwargs["cwd"] == recovery_drill.ROOT and kwargs["shell"] is False
    assert kwargs["timeout"] == recovery_drill.VALIDATOR_TIMEOUT_SECONDS
    assert older.exists() and newest.exists()


def test_inventory_failures_fail_closed(tmp_path):
    missing = recovery_drill.run_scheduled_restore_drill(tmp_path / "missing", artifact=tmp_path / "missing.json")
    assert missing["error_code"] == "backup_directory_missing"

    empty = tmp_path / "empty"
    empty.mkdir()
    assert recovery_drill.run_scheduled_restore_drill(empty, artifact=tmp_path / "empty.json")["error_code"] == "backup_missing"

    invalid = tmp_path / "invalid"
    invalid.mkdir()
    (invalid / "scheduled-20260922T120000000000Z.sqlite3").write_bytes(b"missing metadata")
    assert recovery_drill.run_scheduled_restore_drill(invalid, artifact=tmp_path / "invalid.json")["error_code"] == "backup_inventory_invalid"


def test_validator_process_timeout_and_artifact_failures_fail_closed(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    _scheduled_backup(backups, NOW)
    artifact = tmp_path / "status.json"

    _run_with_validator(monkeypatch, _validator_pass(error_code="validator_problem"), returncode=2)
    nonzero = recovery_drill.run_scheduled_restore_drill(backups, artifact=artifact, git_head="test-head")
    assert nonzero["error_code"] == "validator_process_failed"
    assert nonzero["validation"]["error_code"] == "validator_problem"

    _run_with_validator(monkeypatch, timeout=True)
    assert recovery_drill.run_scheduled_restore_drill(backups, artifact=artifact, git_head="test-head")["error_code"] == "validator_timeout"

    _run_with_validator(monkeypatch)
    assert recovery_drill.run_scheduled_restore_drill(backups, artifact=artifact, git_head="test-head")["error_code"] == "validator_artifact_missing"

    _run_with_validator(monkeypatch, "{")
    assert recovery_drill.run_scheduled_restore_drill(backups, artifact=artifact, git_head="test-head")["error_code"] == "validator_artifact_invalid"


def test_validator_failure_and_inconsistent_required_fields_fail_closed(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    _scheduled_backup(backups, NOW)
    artifact = tmp_path / "status.json"

    _run_with_validator(monkeypatch, _validator_pass(recovery_validation="FAIL", error_code="health_failed"))
    failed = recovery_drill.run_scheduled_restore_drill(backups, artifact=artifact, git_head="test-head")
    assert failed["error_code"] == "recovery_validation_failed"
    assert failed["validation"]["error_code"] == "health_failed"

    _run_with_validator(monkeypatch, _validator_pass(quick_check="failed"))
    assert recovery_drill.run_scheduled_restore_drill(backups, artifact=artifact, git_head="test-head")["error_code"] == "recovery_validation_failed"


def test_final_artifact_is_atomic_private_and_temporary_validator_artifact_is_removed(tmp_path, monkeypatch):
    backups = tmp_path / "backups"
    _scheduled_backup(backups, NOW)
    final = tmp_path / "artifact" / "status.json"
    final.parent.mkdir()
    final.write_text('{"old": true}', encoding="utf-8")
    calls = _run_with_validator(monkeypatch, _validator_pass())

    result = recovery_drill.run_scheduled_restore_drill(backups, artifact=final, git_head="test-head")

    assert json.loads(final.read_text(encoding="utf-8")) == result
    assert stat.S_IMODE(final.stat().st_mode) == 0o600
    assert not list(final.parent.glob(".status.json.*"))
    temporary = Path(calls[0][0][calls[0][0].index("--artifact") + 1])
    assert not temporary.exists() and not temporary.parent.exists()


def test_source_database_and_selected_backup_are_unchanged(tmp_path, sample_markets, monkeypatch):
    from test_database_backup import make_db

    source, backups = tmp_path / "source.db", tmp_path / "backups"
    make_db(source, sample_markets)
    backups.mkdir()
    backup = backups / scheduled_backup_filename(NOW)
    create_backup(source, backup)
    metadata = json.loads(metadata_path(backup).read_text(encoding="utf-8"))
    metadata["created_at"] = NOW.isoformat()
    metadata_path(backup).write_text(json.dumps(metadata), encoding="utf-8")
    source_before = hashlib.sha256(source.read_bytes()).hexdigest()
    backup_before = backup.read_bytes()
    sidecar_before = metadata_path(backup).read_bytes()
    _run_with_validator(monkeypatch, _validator_pass())

    result = recovery_drill.run_scheduled_restore_drill(backups, artifact=tmp_path / "status.json", git_head="test-head")

    assert result["drill_status"] == "PASS"
    assert hashlib.sha256(source.read_bytes()).hexdigest() == source_before
    assert backup.read_bytes() == backup_before
    assert metadata_path(backup).read_bytes() == sidecar_before


def test_cli_exit_codes(monkeypatch, tmp_path, capsys):
    monkeypatch.setattr(drill_cli, "run_scheduled_restore_drill", lambda *_args, **_kwargs: {"drill_status": "PASS"})
    assert drill_cli.main(["--backup-directory", str(tmp_path), "--artifact", str(tmp_path / "status.json"), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["drill_status"] == "PASS"

    monkeypatch.setattr(drill_cli, "run_scheduled_restore_drill", lambda *_args, **_kwargs: {"drill_status": "FAIL"})
    assert drill_cli.main(["--backup-directory", str(tmp_path), "--artifact", str(tmp_path / "status.json")]) == 2
    assert capsys.readouterr().out.strip() == "FAIL"
