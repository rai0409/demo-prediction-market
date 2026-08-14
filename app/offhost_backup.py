"""Fail-closed replication of verified scheduled backup pairs to Amazon S3."""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
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
from typing import Any, BinaryIO, Iterator

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

from app.backup_retention import backup_inventory
from app.database_backup import metadata_path


MAX_SINGLE_PUT_BYTES = 5_000_000_000
LOCK_NAME = ".offhost-backup.lock"
RECEIPTS_DIRECTORY = "receipts"
STATE_NAME = "last-run.json"
OWNER_RE = re.compile(r"\d{12}$")


class OffhostBackupError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class OffhostConfig:
    enabled: bool
    bucket: str
    prefix: str
    region: str
    expected_bucket_owner: str

    @classmethod
    def from_env(cls, environ: dict[str, str] | None = None) -> "OffhostConfig":
        values = os.environ if environ is None else environ
        enabled = values.get("DEMO_OFFHOST_BACKUP_ENABLED", "0").strip() in {"1", "true", "yes", "on"}
        return cls(
            enabled=enabled,
            bucket=values.get("DEMO_OFFHOST_S3_BUCKET", "").strip(),
            prefix=values.get("DEMO_OFFHOST_S3_PREFIX", "demo-prediction-market").strip(),
            region=values.get("DEMO_OFFHOST_S3_REGION", "").strip(),
            expected_bucket_owner=values.get("DEMO_OFFHOST_S3_EXPECTED_BUCKET_OWNER", "").strip(),
        )

    def validate(self) -> None:
        if not self.bucket:
            raise OffhostBackupError("offhost_bucket_missing")
        if not self.region:
            raise OffhostBackupError("offhost_region_missing")
        if not OWNER_RE.fullmatch(self.expected_bucket_owner):
            raise OffhostBackupError("offhost_expected_bucket_owner_invalid")
        _normalise_prefix(self.prefix)


def _normalise_prefix(value: str) -> str:
    prefix = value.strip().strip("/")
    if not prefix or "\\" in prefix or any(ord(char) < 32 for char in prefix):
        raise OffhostBackupError("offhost_prefix_invalid")
    if any(part == ".." for part in prefix.split("/")):
        raise OffhostBackupError("offhost_prefix_invalid")
    return prefix


def _sha256_bytes(value: bytes) -> tuple[str, str]:
    digest = hashlib.sha256(value).digest()
    return digest.hex(), base64.b64encode(digest).decode("ascii")


def sha256_hex_to_base64(value: str) -> str:
    try:
        raw = bytes.fromhex(value)
    except ValueError as exc:
        raise OffhostBackupError("local_backup_changed") from exc
    if len(raw) != 32:
        raise OffhostBackupError("local_backup_changed")
    return base64.b64encode(raw).decode("ascii")


def object_keys(config: OffhostConfig, metadata: dict[str, Any], backup_basename: str, metadata_basename: str) -> tuple[str, str]:
    backup_id = metadata.get("backup_id")
    if not isinstance(backup_id, str) or not backup_id:
        raise OffhostBackupError("invalid_local_backup_inventory")
    created = metadata.get("created_at")
    if not isinstance(created, str):
        raise OffhostBackupError("invalid_local_backup_inventory")
    try:
        instant = datetime.fromisoformat(created)
    except ValueError as exc:
        raise OffhostBackupError("invalid_local_backup_inventory") from exc
    if instant.tzinfo is None or instant.utcoffset() is None:
        raise OffhostBackupError("invalid_local_backup_inventory")
    day = instant.astimezone(timezone.utc)
    root = f"{config.prefix}/backups/{day:%Y/%m/%d}/{backup_id}"
    return f"{root}/{backup_basename}", f"{root}/{metadata_basename}"


def _set_mode(path: Path, mode: int) -> None:
    os.chmod(path, mode)
    if stat.S_IMODE(path.stat().st_mode) != mode:
        raise OffhostBackupError("permission_failed")


def _atomic_json(path: Path, payload: dict[str, Any]) -> None:
    fd, raw = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    temp = Path(raw)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        _set_mode(temp, 0o600)
        os.replace(temp, path)
        _set_mode(path, 0o600)
    finally:
        temp.unlink(missing_ok=True)


@contextmanager
def _lock(directory: Path) -> Iterator[None]:
    path = directory / LOCK_NAME
    with path.open("a", encoding="utf-8") as handle:
        _set_mode(path, 0o600)
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise OffhostBackupError("offhost_run_locked") from None
        try:
            yield
        finally:
            fcntl.flock(handle, fcntl.LOCK_UN)


def _client(config: OffhostConfig):
    return boto3.client(
        "s3",
        region_name=config.region,
        config=Config(connect_timeout=5, read_timeout=60, retries={"mode": "standard", "max_attempts": 3}),
    )


def _client_code(exc: ClientError) -> str:
    return str(exc.response.get("Error", {}).get("Code", "client_error"))


def _head(client: Any, config: OffhostConfig, key: str) -> dict[str, Any]:
    try:
        return client.head_object(Bucket=config.bucket, Key=key, ExpectedBucketOwner=config.expected_bucket_owner, ChecksumMode="ENABLED")
    except ClientError as exc:
        raise OffhostBackupError(f"s3_{_client_code(exc).lower()}") from None


def _verify_head(head: dict[str, Any], expected_checksum: str, expected_size: int, backup_id: str, expected_hex: str) -> None:
    if (
        head.get("ChecksumSHA256") != expected_checksum
        or head.get("ContentLength") != expected_size
        or head.get("ServerSideEncryption") != "AES256"
        or head.get("Metadata", {}).get("backup-id") != backup_id
        or head.get("Metadata", {}).get("sha256") != expected_hex
    ):
        raise OffhostBackupError("remote_object_conflict")


def _put_and_verify(client: Any, config: OffhostConfig, *, key: str, body: BinaryIO | bytes, size: int, checksum: str, digest_hex: str, backup_id: str, content_type: str) -> str:
    parameters = {
        "Bucket": config.bucket,
        "Key": key,
        "Body": body,
        "ContentLength": size,
        "ContentType": content_type,
        "ChecksumSHA256": checksum,
        "IfNoneMatch": "*",
        "ServerSideEncryption": "AES256",
        "ExpectedBucketOwner": config.expected_bucket_owner,
        "Metadata": {"backup-id": backup_id, "sha256": digest_hex},
    }
    for attempt in range(3):
        try:
            if hasattr(body, "seek"):
                body.seek(0)
            client.put_object(**parameters)
            _verify_head(_head(client, config, key), checksum, size, backup_id, digest_hex)
            return "VERIFIED"
        except ClientError as exc:
            code = _client_code(exc)
            if code == "412":
                _verify_head(_head(client, config, key), checksum, size, backup_id, digest_hex)
                return "ALREADY_VERIFIED"
            if code == "409" and attempt < 2:
                continue
            if code == "409":
                raise OffhostBackupError("remote_conditional_retry_exhausted") from None
            raise OffhostBackupError(f"s3_{code.lower()}") from None
    raise OffhostBackupError("remote_conditional_retry_exhausted")


def _read_backup(path: Path, expected_hex: str) -> tuple[BinaryIO, int, str]:
    try:
        handle = path.open("rb")
    except OSError as exc:
        raise OffhostBackupError("local_backup_changed") from exc
    try:
        before = os.fstat(handle.fileno()).st_size
        if before > MAX_SINGLE_PUT_BYTES:
            raise OffhostBackupError("offhost_object_too_large")
        digest = hashlib.sha256()
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
        if digest.hexdigest() != expected_hex or os.fstat(handle.fileno()).st_size != before:
            raise OffhostBackupError("local_backup_changed")
        handle.seek(0)
        return handle, before, sha256_hex_to_base64(expected_hex)
    except Exception:
        handle.close()
        raise


def _receipt(path: Path, payload: dict[str, Any]) -> None:
    _atomic_json(path, payload)


def _initial_state(config: OffhostConfig) -> dict[str, Any]:
    return {
        "status": "FAIL", "replication_status": "NOT_RUN", "started_at": datetime.now(timezone.utc).isoformat(), "completed_at": None,
        "git_head": None, "provider": "s3", "bucket": config.bucket, "prefix": config.prefix, "region": config.region,
        "expected_bucket_owner": config.expected_bucket_owner, "eligible_count": 0, "verified_count": 0, "uploaded_count": 0,
        "already_verified_count": 0, "receipt_count": 0, "verified_backup_ids": [], "failed_backup_id": None,
        "invalid_candidates": [], "offhost_protection_available": False, "error_code": None,
    }


def run_offhost_replication(directory: str | Path, state_directory: str | Path, config: OffhostConfig, *, client: Any | None = None) -> dict[str, Any]:
    """Replicate every eligible local pair oldest-first without remote deletion or listing."""
    local_directory, state_dir = Path(directory), Path(state_directory)
    state_dir.mkdir(parents=True, exist_ok=True)
    _set_mode(state_dir, 0o700)
    receipts = state_dir / RECEIPTS_DIRECTORY
    receipts.mkdir(exist_ok=True)
    _set_mode(receipts, 0o700)
    state = _initial_state(config)
    try:
        with _lock(state_dir):
            if not config.enabled:
                raise OffhostBackupError("offhost_backup_disabled")
            config.validate()
            eligible, invalid = backup_inventory(local_directory)
            state["invalid_candidates"] = invalid
            if invalid:
                raise OffhostBackupError("invalid_local_backup_inventory")
            state["eligible_count"] = len(eligible)
            if not eligible:
                state.update({"status": "PASS", "replication_status": "EMPTY"})
                return state
            s3 = client if client is not None else _client(config)
            for _, backup_path, metadata in reversed(eligible):
                backup_id = metadata["backup_id"]
                state["failed_backup_id"] = backup_id
                receipt_path = receipts / f"{backup_id}.json"
                if receipt_path.exists():
                    try:
                        existing_receipt = json.loads(receipt_path.read_text(encoding="utf-8"))
                    except (OSError, json.JSONDecodeError):
                        raise OffhostBackupError("invalid_receipt") from None
                    if existing_receipt.get("status") != "VERIFIED" or existing_receipt.get("backup_id") != backup_id:
                        raise OffhostBackupError("invalid_receipt")
                db_key, metadata_key = object_keys(config, metadata, backup_path.name, metadata_path(backup_path).name)
                handle, db_size, db_checksum = _read_backup(backup_path, metadata["backup_db_sha256"])
                try:
                    db_status = _put_and_verify(s3, config, key=db_key, body=handle, size=db_size, checksum=db_checksum, digest_hex=metadata["backup_db_sha256"], backup_id=backup_id, content_type="application/vnd.sqlite3")
                finally:
                    handle.close()
                try:
                    metadata_bytes = metadata_path(backup_path).read_bytes()
                except OSError as exc:
                    raise OffhostBackupError("local_backup_changed") from exc
                meta_hex, meta_checksum = _sha256_bytes(metadata_bytes)
                meta_status = _put_and_verify(s3, config, key=metadata_key, body=metadata_bytes, size=len(metadata_bytes), checksum=meta_checksum, digest_hex=meta_hex, backup_id=backup_id, content_type="application/json")
                now = datetime.now(timezone.utc).isoformat()
                receipt = {
                    "status": "VERIFIED", "provider": "s3", "backup_id": backup_id, "backup_basename": backup_path.name,
                    "backup_created_at": metadata["created_at"], "local_backup_sha256": metadata["backup_db_sha256"], "local_backup_size_bytes": db_size,
                    "local_metadata_sha256": meta_hex, "local_metadata_size_bytes": len(metadata_bytes), "remote_backup_key": db_key,
                    "remote_metadata_key": metadata_key, "remote_backup_checksum_sha256": db_checksum,
                    "remote_metadata_checksum_sha256": meta_checksum, "remote_backup_size_bytes": db_size,
                    "remote_metadata_size_bytes": len(metadata_bytes), "uploaded_at": now, "verified_at": now,
                    "backup_object_status": db_status, "metadata_object_status": meta_status, "error_code": None,
                }
                _receipt(receipt_path, receipt)
                state["verified_count"] += 1
                state["uploaded_count"] += int(db_status == "VERIFIED") + int(meta_status == "VERIFIED")
                state["already_verified_count"] += int(db_status == "ALREADY_VERIFIED") + int(meta_status == "ALREADY_VERIFIED")
                state["receipt_count"] += 1
                state["verified_backup_ids"].append(backup_id)
                state["failed_backup_id"] = None
            state.update({"status": "PASS", "replication_status": "VERIFIED", "offhost_protection_available": True})
    except OffhostBackupError as exc:
        state["error_code"] = exc.code
    except (OSError, ClientError):
        state["error_code"] = "offhost_operational_failure"
    finally:
        state["completed_at"] = datetime.now(timezone.utc).isoformat()
        _atomic_json(state_dir / STATE_NAME, state)
    return state
