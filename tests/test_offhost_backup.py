import base64
import fcntl
import io
import json
import stat
from pathlib import Path

import boto3
import pytest
from botocore.stub import ANY, Stubber
from botocore.exceptions import ClientError, EndpointConnectionError

from app.backup_retention import run_scheduled_backup
from app.offhost_backup import (
    LOCK_NAME,
    OffhostConfig,
    MAX_SINGLE_PUT_BYTES,
    OffhostBackupError,
    _put_and_verify,
    _atomic_json,
    _read_backup,
    object_keys,
    run_offhost_replication,
    sha256_hex_to_base64,
)
from test_database_backup import make_db


def _config():
    return OffhostConfig(True, "unit-bucket", "demo-prediction-market", "ap-northeast-1", "123456789012")


def _client():
    return boto3.client("s3", region_name="ap-northeast-1", aws_access_key_id="test", aws_secret_access_key="test")


def _pair(tmp_path, sample_markets):
    source, directory = tmp_path / "source.sqlite3", tmp_path / "backups"
    make_db(source, sample_markets)
    scheduled = run_scheduled_backup(source, directory)
    backup = directory / scheduled["backup_basename"]
    metadata = json.loads(backup.with_suffix(backup.suffix + ".metadata.json").read_text())
    return directory, backup, metadata


def _head(checksum, size, backup_id, digest):
    return {"ChecksumSHA256": checksum, "ContentLength": size, "ServerSideEncryption": "AES256", "Metadata": {"backup-id": backup_id, "sha256": digest}}


def _expect_pair(stubber, config, backup, metadata):
    sidecar = backup.with_suffix(backup.suffix + ".metadata.json")
    db_checksum = sha256_hex_to_base64(metadata["backup_db_sha256"])
    meta_bytes = sidecar.read_bytes()
    import hashlib
    meta_hex = hashlib.sha256(meta_bytes).hexdigest()
    meta_checksum = base64.b64encode(hashlib.sha256(meta_bytes).digest()).decode()
    db_key, meta_key = object_keys(config, metadata, backup.name, sidecar.name)
    common = {"Bucket": config.bucket, "IfNoneMatch": "*", "ServerSideEncryption": "AES256", "ExpectedBucketOwner": config.expected_bucket_owner, "Metadata": {"backup-id": metadata["backup_id"], "sha256": metadata["backup_db_sha256"]}}
    stubber.add_response("put_object", {"ETag": "ignored"}, {**common, "Key": db_key, "Body": ANY, "ContentLength": backup.stat().st_size, "ContentType": "application/vnd.sqlite3", "ChecksumSHA256": db_checksum})
    stubber.add_response("head_object", _head(db_checksum, backup.stat().st_size, metadata["backup_id"], metadata["backup_db_sha256"]), {"Bucket": config.bucket, "Key": db_key, "ExpectedBucketOwner": config.expected_bucket_owner, "ChecksumMode": "ENABLED"})
    stubber.add_response("put_object", {"ETag": "ignored"}, {"Bucket": config.bucket, "Key": meta_key, "Body": ANY, "ContentLength": len(meta_bytes), "ContentType": "application/json", "ChecksumSHA256": meta_checksum, "IfNoneMatch": "*", "ServerSideEncryption": "AES256", "ExpectedBucketOwner": config.expected_bucket_owner, "Metadata": {"backup-id": metadata["backup_id"], "sha256": meta_hex}})
    stubber.add_response("head_object", _head(meta_checksum, len(meta_bytes), metadata["backup_id"], meta_hex), {"Bucket": config.bucket, "Key": meta_key, "ExpectedBucketOwner": config.expected_bucket_owner, "ChecksumMode": "ENABLED"})


def test_disabled_and_invalid_configuration_fail_without_s3(tmp_path):
    disabled = OffhostConfig(False, "", "demo-prediction-market", "", "")
    result = run_offhost_replication(tmp_path / "backups", tmp_path / "state", disabled)
    assert result["status"] == "FAIL" and result["error_code"] == "offhost_backup_disabled"
    invalid = OffhostConfig.from_env({"DEMO_OFFHOST_BACKUP_ENABLED": "1", "DEMO_OFFHOST_S3_BUCKET": "unit-bucket", "DEMO_OFFHOST_S3_REGION": "ap-northeast-1", "DEMO_OFFHOST_S3_EXPECTED_BUCKET_OWNER": "123456789012", "DEMO_OFFHOST_S3_PREFIX": "../bad"})
    result = run_offhost_replication(tmp_path / "backups", tmp_path / "invalid-state", invalid)
    assert result["status"] == "FAIL" and result["error_code"] == "offhost_prefix_invalid"


def test_key_and_checksum_are_deterministic(tmp_path, sample_markets):
    _, backup, metadata = _pair(tmp_path, sample_markets)
    config = _config()
    keys = object_keys(config, metadata, backup.name, backup.with_suffix(backup.suffix + ".metadata.json").name)
    assert keys == object_keys(config, metadata, backup.name, backup.with_suffix(backup.suffix + ".metadata.json").name)
    assert f"/{metadata['backup_id']}/{backup.name}" in keys[0]
    assert sha256_hex_to_base64("00" * 32) == base64.b64encode(bytes(32)).decode()
    normalised = OffhostConfig(True, "unit-bucket", "/foo/", "ap-northeast-1", "123456789012")
    assert object_keys(normalised, metadata, backup.name, backup.with_suffix(backup.suffix + ".metadata.json").name)[0].startswith("foo/backups/")
    assert OffhostConfig.from_env({"DEMO_OFFHOST_BACKUP_ENABLED": "TRUE"}).enabled is True


def test_real_pair_uses_conditional_put_head_and_writes_receipt(tmp_path, sample_markets):
    directory, backup, metadata = _pair(tmp_path, sample_markets)
    client, config = _client(), _config()
    with Stubber(client) as stubber:
        _expect_pair(stubber, config, backup, metadata)
        result = run_offhost_replication(directory, tmp_path / "state", config, client=client)
    receipt = tmp_path / "state" / "receipts" / f"{metadata['backup_id']}.json"
    assert result["status"] == "PASS" and result["replication_status"] == "VERIFIED"
    assert json.loads(receipt.read_text())["status"] == "VERIFIED"
    assert json.loads((tmp_path / "state" / "last-run.json").read_text())["status"] == "PASS"
    assert stat.S_IMODE((tmp_path / "state").stat().st_mode) == 0o700
    assert stat.S_IMODE(receipt.stat().st_mode) == 0o600


def test_invalid_inventory_blocks_remote_calls_and_manual_file_is_ignored(tmp_path, sample_markets):
    directory, _, _ = _pair(tmp_path, sample_markets)
    (directory / "scheduled-broken.sqlite3").write_bytes(b"broken")
    (directory / "manual-backup.sqlite3").write_bytes(b"manual")
    result = run_offhost_replication(directory, tmp_path / "state", _config(), client=object())
    assert result["status"] == "FAIL" and result["error_code"] == "invalid_local_backup_inventory"
    assert (directory / "manual-backup.sqlite3").exists()


def test_precondition_existing_matching_object_is_idempotent(tmp_path, sample_markets):
    directory, backup, metadata = _pair(tmp_path, sample_markets)
    client, config = _client(), _config()
    db_checksum = sha256_hex_to_base64(metadata["backup_db_sha256"])
    db_key, _ = object_keys(config, metadata, backup.name, backup.with_suffix(backup.suffix + ".metadata.json").name)
    with Stubber(client) as stubber:
        stubber.add_client_error("put_object", service_error_code="PreconditionFailed", http_status_code=412, expected_params={"Bucket": config.bucket, "Key": db_key, "Body": ANY, "ContentLength": backup.stat().st_size, "ContentType": "application/vnd.sqlite3", "ChecksumSHA256": db_checksum, "IfNoneMatch": "*", "ServerSideEncryption": "AES256", "ExpectedBucketOwner": config.expected_bucket_owner, "Metadata": {"backup-id": metadata["backup_id"], "sha256": metadata["backup_db_sha256"]}})
        stubber.add_response("head_object", _head(db_checksum, backup.stat().st_size, metadata["backup_id"], metadata["backup_db_sha256"]), {"Bucket": config.bucket, "Key": db_key, "ExpectedBucketOwner": config.expected_bucket_owner, "ChecksumMode": "ENABLED"})
        sidecar = backup.with_suffix(backup.suffix + ".metadata.json")
        import hashlib
        meta_bytes = sidecar.read_bytes(); meta_hex = hashlib.sha256(meta_bytes).hexdigest(); meta_checksum = base64.b64encode(hashlib.sha256(meta_bytes).digest()).decode()
        _, meta_key = object_keys(config, metadata, backup.name, sidecar.name)
        stubber.add_response("put_object", {"ETag": "ignored"}, {"Bucket": config.bucket, "Key": meta_key, "Body": ANY, "ContentLength": len(meta_bytes), "ContentType": "application/json", "ChecksumSHA256": meta_checksum, "IfNoneMatch": "*", "ServerSideEncryption": "AES256", "ExpectedBucketOwner": config.expected_bucket_owner, "Metadata": {"backup-id": metadata["backup_id"], "sha256": meta_hex}})
        stubber.add_response("head_object", _head(meta_checksum, len(meta_bytes), metadata["backup_id"], meta_hex), {"Bucket": config.bucket, "Key": meta_key, "ExpectedBucketOwner": config.expected_bucket_owner, "ChecksumMode": "ENABLED"})
        result = run_offhost_replication(directory, tmp_path / "state", config, client=client)
    assert result["status"] == "PASS" and result["already_verified_count"] == 1


def test_lock_and_forbidden_remote_apis_are_fail_closed(tmp_path):
    state = tmp_path / "state"; state.mkdir()
    lock = (state / LOCK_NAME).open("a"); fcntl.flock(lock, fcntl.LOCK_EX)
    try:
        result = run_offhost_replication(tmp_path / "backups", state, _config())
    finally:
        lock.close()
    assert result["error_code"] == "offhost_run_locked"
    source = Path("app/offhost_backup.py").read_text()
    for forbidden in ("delete_object(", "delete_objects(", "list_objects(", "list_objects_v2("):
        assert forbidden not in source
    assert MAX_SINGLE_PUT_BYTES == 5_000_000_000


def test_conflicting_412_and_bounded_409_are_fail_closed_or_retried():
    class ConflictClient:
        def put_object(self, **_):
            raise ClientError({"Error": {"Code": "412"}}, "PutObject")
        def head_object(self, **_):
            return _head("wrong", 1, "backup", "wrong")

    with pytest.raises(OffhostBackupError, match="remote_object_conflict"):
        _put_and_verify(ConflictClient(), _config(), key="key", body=io.BytesIO(b"x"), size=1, checksum="checksum", digest_hex="a" * 64, backup_id="backup", content_type="application/json")

    class RetryClient:
        attempts = 0
        def put_object(self, **_):
            self.attempts += 1
            if self.attempts < 3:
                raise ClientError({"Error": {"Code": "ConditionalRequestConflict"}, "ResponseMetadata": {"HTTPStatusCode": 409}}, "PutObject")
        def head_object(self, **_):
            return _head("checksum", 1, "backup", "a" * 64)

    retry = RetryClient()
    assert _put_and_verify(retry, _config(), key="key", body=io.BytesIO(b"x"), size=1, checksum="checksum", digest_hex="a" * 64, backup_id="backup", content_type="application/json") == "VERIFIED"
    assert retry.attempts == 3


def test_symbolic_conditional_errors_and_http_fallback_are_classified():
    from app.offhost_backup import classify_conditional_put_error
    assert classify_conditional_put_error(ClientError({"Error": {"Code": "PreconditionFailed"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")) == "precondition"
    assert classify_conditional_put_error(ClientError({"Error": {"Code": "ConditionalRequestConflict"}, "ResponseMetadata": {"HTTPStatusCode": 409}}, "PutObject")) == "conditional"
    assert classify_conditional_put_error(ClientError({"Error": {"Code": "other"}, "ResponseMetadata": {"HTTPStatusCode": 412}}, "PutObject")) == "precondition"


def test_metadata_snapshot_mismatch_blocks_remote_calls(tmp_path, sample_markets, monkeypatch):
    directory, backup, metadata = _pair(tmp_path, sample_markets)
    sidecar = backup.with_suffix(backup.suffix + ".metadata.json")
    changed = dict(metadata); changed["backup_id"] = "changed"
    sidecar.write_text(json.dumps(changed))
    monkeypatch.setattr("app.offhost_backup.backup_inventory", lambda _: ([(__import__("datetime").datetime.now(__import__("datetime").timezone.utc), backup, metadata)], []))
    result = run_offhost_replication(directory, tmp_path / "state", _config(), client=object())
    assert result["status"] == "FAIL" and result["error_code"] == "local_backup_changed"


def test_metadata_mutation_after_db_put_uses_pre_remote_snapshot(tmp_path, sample_markets):
    directory, backup, metadata = _pair(tmp_path, sample_markets)
    sidecar = backup.with_suffix(backup.suffix + ".metadata.json")
    original = sidecar.read_bytes()
    class Client:
        puts = []
        def put_object(self, **kwargs):
            self.puts.append(kwargs)
            if len(self.puts) == 1:
                sidecar.write_text('{"changed": true}')
        def head_object(self, **kwargs):
            item = self.puts[-1]
            return _head(item["ChecksumSHA256"], item["ContentLength"], item["Metadata"]["backup-id"], item["Metadata"]["sha256"])
    client = Client()
    assert run_offhost_replication(directory, tmp_path / "state", _config(), client=client)["status"] == "PASS"
    assert client.puts[1]["Body"] == original


def test_transport_access_denied_and_missing_local_are_structured_failures(tmp_path, sample_markets, monkeypatch):
    directory, backup, _ = _pair(tmp_path, sample_markets)
    class TransportClient:
        def put_object(self, **_):
            raise EndpointConnectionError(endpoint_url="https://s3.invalid")
    transport = run_offhost_replication(directory, tmp_path / "transport", _config(), client=TransportClient())
    assert transport["status"] == "FAIL" and transport["error_code"] == "offhost_operational_failure"
    assert json.loads((tmp_path / "transport" / "last-run.json").read_text())["status"] == "FAIL"
    class DeniedClient:
        def put_object(self, **_):
            raise ClientError({"Error": {"Code": "AccessDenied"}, "ResponseMetadata": {"HTTPStatusCode": 403}}, "PutObject")
    denied = run_offhost_replication(directory, tmp_path / "denied", _config(), client=DeniedClient())
    assert denied["status"] == "FAIL" and denied["error_code"] == "s3_accessdenied"
    original = __import__("app.offhost_backup", fromlist=["_read_backup"])._read_backup
    def remove_then_read(path, digest):
        path.unlink()
        return original(path, digest)
    monkeypatch.setattr("app.offhost_backup._read_backup", remove_then_read)
    missing = run_offhost_replication(directory, tmp_path / "missing", _config(), client=object())
    assert missing["status"] == "FAIL" and missing["error_code"] == "local_backup_changed"


def test_size_gate_atomic_artifacts_and_secret_safety(tmp_path, sample_markets, monkeypatch):
    huge = tmp_path / "huge.sqlite3"
    with huge.open("wb") as handle:
        handle.truncate(MAX_SINGLE_PUT_BYTES + 1)
    with pytest.raises(OffhostBackupError, match="offhost_object_too_large"):
        _read_backup(huge, "0" * 64)
    final = tmp_path / "last-run.json"; final.write_text('{"old": true}')
    monkeypatch.setattr("app.offhost_backup.os.replace", lambda *_: (_ for _ in ()).throw(OSError("fail")))
    with pytest.raises(OSError):
        _atomic_json(final, {"new": True})
    assert json.loads(final.read_text()) == {"old": True}
    assert not list(tmp_path.glob(".last-run.json.*"))
    monkeypatch.undo()
    pair_root = tmp_path / "pair"; pair_root.mkdir()
    directory, _, _ = _pair(pair_root, sample_markets)
    result = run_offhost_replication(directory, tmp_path / "state", OffhostConfig(False, "", "demo-prediction-market", "", ""))
    payload = json.dumps(result) + (tmp_path / "state" / "last-run.json").read_text()
    for forbidden in ("AWS_SECRET_ACCESS_KEY", "AWS_SESSION_TOKEN", "Authorization", "X-Amz-Signature", "password"):
        assert forbidden not in payload


def test_malformed_receipt_is_fail_closed(tmp_path, sample_markets):
    directory, _, metadata = _pair(tmp_path, sample_markets)
    receipts = tmp_path / "state" / "receipts"
    receipts.mkdir(parents=True)
    (receipts / f"{metadata['backup_id']}.json").write_text("{")

    result = run_offhost_replication(directory, tmp_path / "state", _config(), client=object())

    assert result["status"] == "FAIL" and result["error_code"] == "invalid_receipt"
