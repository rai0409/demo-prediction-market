import importlib.util
import json
from pathlib import Path

from app.database_backup import create_backup, metadata_path
from app.storage import CURRENT_SCHEMA_VERSION

SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "validate_recovery.py"
SPEC = importlib.util.spec_from_file_location("recovery_validation", SCRIPT_PATH)
recovery = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(recovery)


def test_recovery_validation_restores_and_writes_a_readable_artifact(tmp_path, sample_markets, monkeypatch):
    from test_database_backup import make_db
    source, backup, artifact = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "artifact.json"
    make_db(source, sample_markets)
    create_backup(source, backup)
    monkeypatch.setattr(recovery, "_runtime", lambda *_args: (True, True))
    result = recovery.validate_recovery(backup, artifact)
    assert result["recovery_validation"] == "PASS"
    assert result["quick_check"] == "ok"
    assert result["foreign_key_check_rows"] == 0
    assert result["post_restore_schema_version"] == CURRENT_SCHEMA_VERSION
    assert result["collateral_invariants"] == "verified"
    assert result["post_restore_health"] is True and result["post_restore_ready"] is True
    assert json.loads(artifact.read_text()) == result


def test_recovery_validation_rejects_backup_hash_mismatch(tmp_path, sample_markets):
    from test_database_backup import make_db
    source, backup, artifact = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "artifact.json"
    make_db(source, sample_markets); create_backup(source, backup)
    metadata = json.loads(metadata_path(backup).read_text()); metadata["backup_db_sha256"] = "0" * 64
    metadata_path(backup).write_text(json.dumps(metadata))
    result = recovery.validate_recovery(backup, artifact)
    assert result["recovery_validation"] == "FAIL"
    assert result["error_code"] == "backup_hash_mismatch"


def test_recovery_validation_rejects_future_schema_metadata(tmp_path, sample_markets):
    from test_database_backup import make_db
    source, backup, artifact = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "artifact.json"
    make_db(source, sample_markets); create_backup(source, backup)
    metadata = json.loads(metadata_path(backup).read_text()); metadata["schema_version"] = CURRENT_SCHEMA_VERSION + 1
    metadata_path(backup).write_text(json.dumps(metadata))
    result = recovery.validate_recovery(backup, artifact)
    assert result["recovery_validation"] == "FAIL"
    assert result["error_code"] == "schema_unsupported"


def test_recovery_validation_rejects_truncated_backup(tmp_path, sample_markets):
    from test_database_backup import make_db
    source, backup, artifact = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "artifact.json"
    make_db(source, sample_markets); create_backup(source, backup)
    backup.write_bytes(backup.read_bytes()[:100])
    result = recovery.validate_recovery(backup, artifact)
    assert result["recovery_validation"] == "FAIL"
    assert result["error_code"] == "backup_hash_mismatch"


def test_recovery_validation_rejects_health_and_ready_failures_separately(tmp_path, sample_markets, monkeypatch):
    from test_database_backup import make_db
    source, backup = tmp_path / "source.db", tmp_path / "backup.sqlite"
    make_db(source, sample_markets); create_backup(source, backup)
    monkeypatch.setattr(recovery, "_runtime", lambda *_args: (False, False))
    health = recovery.validate_recovery(backup, tmp_path / "health.json")
    assert health["recovery_validation"] == "FAIL" and health["error_code"] == "health_failed"
    monkeypatch.setattr(recovery, "_runtime", lambda *_args: (True, False))
    ready = recovery.validate_recovery(backup, tmp_path / "ready.json")
    assert ready["recovery_validation"] == "FAIL" and ready["error_code"] == "ready_failed"
