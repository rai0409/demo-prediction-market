import fcntl
import base64
import hashlib
import json
import os
import stat
from datetime import datetime, timedelta, timezone

import pytest

import app.backup_retention as backup_retention
from app.database_backup import BackupError, _sha256, create_backup, metadata_path
from test_database_backup import make_db


def _source(tmp_path, sample_markets):
    source = tmp_path / "source.db"
    make_db(source, sample_markets)
    return source


def _historical_backup(source, directory, instant):
    backup = directory / backup_retention.scheduled_backup_filename(instant)
    create_backup(source, backup)
    sidecar = metadata_path(backup)
    metadata = json.loads(sidecar.read_text())
    metadata["created_at"] = instant.isoformat()
    sidecar.write_text(json.dumps(metadata))
    return backup


def _verified_receipt(receipts, backup):
    receipts.mkdir(exist_ok=True); os.chmod(receipts, 0o700)
    sidecar = metadata_path(backup); metadata = json.loads(sidecar.read_text()); raw = sidecar.read_bytes()
    meta_hex = hashlib.sha256(raw).hexdigest(); meta_b64 = base64.b64encode(hashlib.sha256(raw).digest()).decode()
    backup_b64 = base64.b64encode(bytes.fromhex(metadata["backup_db_sha256"])).decode(); backup_id = metadata["backup_id"]; now = datetime.now(timezone.utc).isoformat()
    receipt = {"status":"VERIFIED","provider":"s3","backup_id":backup_id,"backup_basename":backup.name,"backup_created_at":metadata["created_at"],"local_backup_sha256":metadata["backup_db_sha256"],"local_backup_size_bytes":backup.stat().st_size,"local_metadata_sha256":meta_hex,"local_metadata_size_bytes":len(raw),"remote_backup_key":f"x/{backup_id}/{backup.name}","remote_metadata_key":f"x/{backup_id}/{sidecar.name}","remote_backup_checksum_sha256":backup_b64,"remote_metadata_checksum_sha256":meta_b64,"remote_backup_size_bytes":backup.stat().st_size,"remote_metadata_size_bytes":len(raw),"uploaded_at":now,"verified_at":now,"backup_object_status":"VERIFIED","metadata_object_status":"VERIFIED","error_code":None}
    path = receipts / f"{backup_id}.json"; path.write_text(json.dumps(receipt)); os.chmod(path, 0o600)
    return path


def test_scheduled_run_creates_secure_backup_and_state(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"

    result = backup_retention.run_scheduled_backup(source, directory)

    assert result["status"] == "PASS"
    assert result["backup_created"] is True
    backup = directory / result["backup_basename"]
    sidecar = metadata_path(backup)
    state_path = directory / "last-run.json"
    assert backup_retention.SCHEDULED_NAME.fullmatch(backup.name)
    assert backup.is_file() and sidecar.is_file() and state_path.is_file()
    assert _sha256(backup) == result["backup_db_sha256"]
    assert json.loads(state_path.read_text()) == result
    assert stat.S_IMODE(directory.stat().st_mode) == 0o700
    assert stat.S_IMODE(backup.stat().st_mode) == 0o600
    assert stat.S_IMODE(sidecar.stat().st_mode) == 0o600
    assert stat.S_IMODE(state_path.stat().st_mode) == 0o600


def test_daily_and_weekly_retention_prunes_only_verified_receipts(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    newest = datetime.now(timezone.utc) - timedelta(days=1)
    historical = [_historical_backup(source, directory, newest - timedelta(days=days)) for days in range(2, 50, 4)]
    receipts = tmp_path / "receipts"
    for backup in historical: _verified_receipt(receipts, backup)

    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=2, weekly_retention=4, offhost_receipts_directory=receipts)
    remaining, invalid = backup_retention.backup_inventory(directory)
    remaining_names = {backup.name for _, backup, _ in remaining}

    assert result["status"] == "PASS" and not invalid
    assert result["backup_basename"] in remaining_names
    assert len(result["kept_daily"]) == 2
    assert len(set(result["kept_daily"] + result["kept_weekly"])) == len(remaining_names)
    assert result["deleted_backups"]
    assert any(not backup.exists() for backup in historical)
    assert all(path.exists() for path in receipts.iterdir())


def test_missing_receipt_holds_prune_candidate_without_failing_backup(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets); directory = tmp_path / "backups"; directory.mkdir()
    old = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=30))
    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=0, weekly_retention=0, offhost_receipts_directory=tmp_path / "missing")
    assert result["status"] == "PASS" and result["retention_status"] == "DEGRADED"
    assert old.exists() and metadata_path(old).exists() and old.name in result["protected_pending_offhost"] and result["deleted_backups"] == []


def test_real_offhost_receipt_authorizes_historical_prune(tmp_path, sample_markets):
    from botocore.stub import Stubber
    from app.offhost_backup import run_offhost_replication
    from test_offhost_backup import _client, _config, _expect_pair
    source = _source(tmp_path, sample_markets); directory = tmp_path / "backups"; directory.mkdir()
    old = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=30))
    metadata = json.loads(metadata_path(old).read_text()); client, config = _client(), _config(); state = tmp_path / "offhost"
    with Stubber(client) as stubber:
        _expect_pair(stubber, config, old, metadata)
        assert run_offhost_replication(directory, state, config, client=client)["status"] == "PASS"
    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=0, weekly_retention=0, offhost_receipts_directory=state / "receipts")
    assert result["status"] == "PASS" and not old.exists() and not metadata_path(old).exists()
    assert (state / "receipts" / f"{metadata['backup_id']}.json").exists()


def test_small_inventory_is_not_pruned(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    existing = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=1))

    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=7, weekly_retention=4)

    assert result["status"] == "PASS"
    assert result["deleted_backups"] == []
    assert existing.exists()


@pytest.mark.parametrize("defect", ["missing", "malformed", "metadata_orphan", "sha_mismatch"])
def test_invalid_scheduler_artifacts_are_never_pruned(tmp_path, sample_markets, defect):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    broken = directory / "scheduled-20260101T000000000000Z.sqlite3"
    if defect == "missing":
        broken.write_bytes(b"missing metadata")
    elif defect == "metadata_orphan":
        metadata_path(broken).write_text("{}")
    else:
        create_backup(source, broken)
        sidecar = metadata_path(broken)
        if defect == "malformed":
            sidecar.write_text("{")
        else:
            metadata = json.loads(sidecar.read_text())
            metadata["backup_db_sha256"] = "0" * 64
            sidecar.write_text(json.dumps(metadata))

    result = backup_retention.run_scheduled_backup(source, directory)

    assert result["status"] == "FAIL"
    assert result["retention_status"] == "DEGRADED"
    retained = metadata_path(broken) if defect == "metadata_orphan" else broken
    assert retained.exists()
    assert retained.name in result["invalid_candidates"]


def test_malformed_scheduler_backup_filename_blocks_pruning(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    old_valid = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=30))
    malformed = directory / "scheduled-broken.sqlite3"
    malformed.write_bytes(b"not a scheduler backup")

    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=0, weekly_retention=0)

    assert result["status"] == "FAIL"
    assert result["retention_status"] == "DEGRADED"
    assert result["error_code"] == "invalid_backup_inventory"
    assert malformed.exists() and malformed.name in result["invalid_candidates"]
    assert old_valid.exists()
    assert result["deleted_backups"] == []


def test_malformed_scheduler_sidecar_filename_blocks_pruning(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    old_valid = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=30))
    malformed = directory / "scheduled-broken.sqlite3.metadata.json"
    malformed.write_text("{}")

    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=0, weekly_retention=0)

    assert result["status"] == "FAIL"
    assert result["retention_status"] == "DEGRADED"
    assert result["error_code"] == "invalid_backup_inventory"
    assert malformed.exists() and malformed.name in result["invalid_candidates"]
    assert old_valid.exists()
    assert result["deleted_backups"] == []


def test_unknown_files_are_not_part_of_retention_inventory(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    unknowns = [directory / "manual-backup.sqlite3", directory / "notes.txt", directory / "random.json"]
    for path in unknowns:
        path.write_text("unmanaged")

    result = backup_retention.run_scheduled_backup(source, directory)

    assert result["status"] == "PASS"
    assert all(path.exists() for path in unknowns)


def test_backup_failure_does_not_prune_existing_backups(tmp_path, sample_markets, monkeypatch):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    existing = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=30))
    monkeypatch.setattr(backup_retention, "create_backup", lambda *args, **kwargs: (_ for _ in ()).throw(BackupError("source_missing")))

    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=0, weekly_retention=0)

    assert result["status"] == "FAIL"
    assert result["deleted_backups"] == []
    assert existing.exists()


def test_delete_failure_is_reported_and_current_backup_survives(tmp_path, sample_markets, monkeypatch):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    old = _historical_backup(source, directory, datetime.now(timezone.utc) - timedelta(days=30))
    receipts = tmp_path / "receipts"; _verified_receipt(receipts, old)
    monkeypatch.setattr(backup_retention, "_delete_backup_pair", lambda _: (_ for _ in ()).throw(OSError("denied")))

    result = backup_retention.run_scheduled_backup(source, directory, daily_retention=0, weekly_retention=0, offhost_receipts_directory=receipts)

    assert result["status"] == "FAIL"
    assert result["error_code"] == "retention_delete_failed"
    assert result["backup_created"] is True
    assert (directory / result["backup_basename"]).exists()
    assert json.loads((directory / "last-run.json").read_text())["error_code"] == "retention_delete_failed"


def test_lock_contention_fails_closed_without_creating_backup(tmp_path, sample_markets):
    source = _source(tmp_path, sample_markets)
    directory = tmp_path / "backups"
    directory.mkdir()
    lock = (directory / backup_retention.LOCK_NAME).open("a")
    fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        result = backup_retention.run_scheduled_backup(source, directory)
    finally:
        lock.close()

    assert result["status"] == "FAIL"
    assert result["error_code"] == "backup_run_locked"
    assert not list(directory.glob("scheduled-*.sqlite3"))


def test_atomic_state_write_preserves_prior_final_and_cleans_temporary_file(tmp_path, monkeypatch):
    final = tmp_path / "last-run.json"
    final.write_text('{"old": true}')
    monkeypatch.setattr(backup_retention.os, "replace", lambda *_: (_ for _ in ()).throw(OSError("replace failed")))

    with pytest.raises(OSError):
        backup_retention._atomic_write_json(final, {"new": True})

    assert json.loads(final.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".last-run.json.*"))
