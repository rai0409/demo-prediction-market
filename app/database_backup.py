from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import os
import sqlite3
import time
from typing import Any
import uuid

from app.collateral_ledger import verify_collateral_invariants
from app.storage import CURRENT_SCHEMA_REQUIRED_TABLES, CURRENT_SCHEMA_VERSION, verify_audit_chain


REQUIRED_TABLES = CURRENT_SCHEMA_REQUIRED_TABLES


class BackupError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def metadata_path(backup_path: Path) -> Path:
    return backup_path.with_suffix(backup_path.suffix + ".metadata.json")


def _open_readonly(path: Path) -> sqlite3.Connection:
    if not path.is_file():
        raise BackupError("source_missing")
    conn = sqlite3.connect(f"file:{path.resolve()}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _integrity(conn: sqlite3.Connection) -> None:
    try:
        if conn.execute("pragma integrity_check").fetchone()[0] != "ok":
            raise BackupError("integrity_failed")
        if conn.execute("pragma quick_check").fetchone()[0] != "ok":
            raise BackupError("quick_check_failed")
        conn.execute("pragma foreign_keys = on")
        if conn.execute("pragma foreign_key_check").fetchone() is not None:
            raise BackupError("foreign_key_failed")
    except sqlite3.DatabaseError:
        raise BackupError("database_error") from None


def _tables(conn: sqlite3.Connection) -> list[str]:
    return [row[0] for row in conn.execute("select name from sqlite_master where type = 'table' and name not like 'sqlite_%' order by name")]


def _schema_hash(conn: sqlite3.Connection) -> str:
    rows = conn.execute("select type, name, tbl_name, coalesce(sql, '') from sqlite_master where type in ('table', 'index', 'trigger', 'view') and name not like 'sqlite_%' order by type, name").fetchall()
    return hashlib.sha256(json.dumps([tuple(row) for row in rows], ensure_ascii=False, separators=(",", ":")).encode()).hexdigest()


def _details(conn: sqlite3.Connection) -> dict[str, Any]:
    _integrity(conn)
    try:
        tables = _tables(conn)
    except sqlite3.DatabaseError:
        raise BackupError("database_error") from None
    missing = REQUIRED_TABLES.difference(tables)
    if missing:
        raise BackupError("schema_incompatible")
    counts = {name: int(conn.execute(f' select count(*) from "{name}" ').fetchone()[0]) for name in tables}
    try:
        audit = verify_audit_chain(conn)
    except sqlite3.DatabaseError:
        raise BackupError("database_error") from None
    if audit["integrity_status"] not in {"empty", "verified"}:
        raise BackupError("audit_integrity_failed")
    try:
        collateral = verify_collateral_invariants(conn)
    except sqlite3.DatabaseError:
        raise BackupError("database_error") from None
    if collateral["integrity_status"] != "verified":
        raise BackupError("collateral_invariant_failed")
    final_row = conn.execute("select event_hash from demo_audit_events order by id desc limit 1").fetchone()
    return {
        "schema_version": int(conn.execute("pragma user_version").fetchone()[0]),
        "schema_hash": _schema_hash(conn),
        "table_counts": counts,
        "audit_integrity": True,
        "audit_event_count": counts.get("demo_audit_events", 0),
        "audit_final_hash": str(final_row[0]) if final_row and final_row[0] else "",
        "sqlite_version": sqlite3.sqlite_version,
        "quick_check_result": "ok",
        "foreign_key_check_rows": 0,
        "audit_chain_result": audit["integrity_status"],
        "collateral_invariant_result": collateral["integrity_status"],
        "table_row_counts": counts,
    }


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _git_head() -> str | None:
    import subprocess
    try:
        result = subprocess.run(["git", "rev-parse", "HEAD"], cwd=Path(__file__).parents[1], capture_output=True, text=True, check=True)
    except (OSError, subprocess.CalledProcessError):
        return None
    return result.stdout.strip() or None


def _temp_path(final_path: Path) -> Path:
    return final_path.with_name(f".{final_path.name}.{uuid.uuid4().hex}.tmp")


def _write_json_atomic(path: Path, value: dict[str, Any]) -> None:
    temp = _temp_path(path)
    try:
        temp.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp, path)
    finally:
        temp.unlink(missing_ok=True)


def create_backup(source: str | Path, output: str | Path, *, overwrite: bool = False) -> dict[str, Any]:
    source_path, output_path = Path(source), Path(output)
    started = time.monotonic()
    if source_path.resolve() == output_path.resolve():
        raise BackupError("same_path")
    if output_path.exists() and not overwrite:
        raise BackupError("output_exists")
    output_path.parent.mkdir(parents=True, exist_ok=True)
    meta_path = metadata_path(output_path)
    if meta_path.exists() and not overwrite:
        raise BackupError("metadata_exists")
    temp = _temp_path(output_path)
    meta_temp: Path | None = None
    published = False
    try:
        with _open_readonly(source_path) as source_conn:
            source_details = _details(source_conn)
            source_sha256 = _sha256(source_path)
            with sqlite3.connect(temp) as backup_conn:
                source_conn.backup(backup_conn)
            with _open_readonly(temp) as backup_conn:
                backup_details = _details(backup_conn)
        if source_details["table_counts"] != backup_details["table_counts"] or source_details["schema_hash"] != backup_details["schema_hash"]:
            raise BackupError("verification_failed")
        created_at = datetime.now(timezone.utc).isoformat()
        metadata = {**backup_details, "status": "success", "created_at": created_at, "source_basename": source_path.name, "backup_basename": output_path.name, "file_size": temp.stat().st_size, "integrity": "ok", "backup_id": str(uuid.uuid4()), "git_head": _git_head(), "backup_size_bytes": temp.stat().st_size, "duration_ms": int((time.monotonic() - started) * 1000), "source_db_sha256": source_sha256, "backup_db_sha256": _sha256(temp)}
        meta_temp = _temp_path(meta_path)
        meta_temp.write_text(json.dumps(metadata, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temp, output_path)
        published = True
        os.replace(meta_temp, meta_path)
        return {**metadata, "elapsed_ms": int((time.monotonic() - started) * 1000), "error_code": None}
    except Exception:
        temp.unlink(missing_ok=True)
        if meta_temp is not None:
            meta_temp.unlink(missing_ok=True)
        if published:
            output_path.unlink(missing_ok=True)
        raise


def restore_backup(backup: str | Path, output: str | Path, *, production_db: str | Path, overwrite: bool = False) -> dict[str, Any]:
    backup_path, output_path, production_path = Path(backup), Path(output), Path(production_db)
    started = time.monotonic()
    if backup_path.resolve() == output_path.resolve():
        raise BackupError("same_path")
    if output_path.resolve() == production_path.resolve():
        raise BackupError("production_output_forbidden")
    if output_path.exists() and not overwrite:
        raise BackupError("output_exists")
    meta_path = metadata_path(backup_path)
    if not meta_path.is_file():
        raise BackupError("metadata_missing")
    try:
        metadata = json.loads(meta_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        raise BackupError("metadata_invalid") from None
    output_path.parent.mkdir(parents=True, exist_ok=True)
    temp = _temp_path(output_path)
    try:
        if metadata.get("backup_db_sha256") is not None and metadata["backup_db_sha256"] != _sha256(backup_path):
            raise BackupError("backup_hash_mismatch")
        with _open_readonly(backup_path) as source_conn:
            source_details = _details(source_conn)
            if source_details["schema_version"] > CURRENT_SCHEMA_VERSION:
                raise BackupError("schema_unsupported")
            for key in ("schema_version", "schema_hash", "table_counts", "audit_final_hash"):
                if metadata.get(key) != source_details.get(key):
                    raise BackupError("metadata_mismatch")
            with sqlite3.connect(temp) as restored_conn:
                source_conn.backup(restored_conn)
        with _open_readonly(temp) as restored_conn:
            restored_details = _details(restored_conn)
        if restored_details["table_counts"] != source_details["table_counts"] or restored_details["schema_hash"] != source_details["schema_hash"]:
            raise BackupError("verification_failed")
        os.replace(temp, output_path)
        return {"status": "success", "restored_basename": output_path.name, "restored_at": datetime.now(timezone.utc).isoformat(), "integrity": "ok", "table_counts_match": True, "audit_integrity": True, "schema_compatible": True, "elapsed_ms": int((time.monotonic() - started) * 1000), "error_code": None}
    except Exception:
        temp.unlink(missing_ok=True)
        raise
