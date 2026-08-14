#!/usr/bin/env python3
"""Run a deterministic, isolated recovery validation drill for a SQLite backup."""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.collateral_ledger import verify_collateral_invariants
from app.database_backup import BackupError, _sha256, metadata_path, restore_backup
from app.storage import CURRENT_SCHEMA_REQUIRED_TABLES, CURRENT_SCHEMA_VERSION, connect, verify_audit_chain


def _atomic_json(path: Path, value: dict[str, Any]) -> None:
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        temporary.write_text(json.dumps(value, ensure_ascii=False, sort_keys=True), encoding="utf-8")
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def _port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _stop(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def _runtime(base_url: str, database: Path) -> tuple[bool, bool]:
    environment = os.environ.copy() | {"DEMO_PREDICTION_DB": str(database), "DEMO_PREDICTION_LIVE": "0", "DEMO_PREDICTION_AUTO_REFRESH": "0", "DEMO_PREDICTION_WS_ENABLED": "0", "DEMO_TRANSLATION_ENABLED": "0"}
    process: subprocess.Popen[str] | None = None
    try:
        process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", base_url.rsplit(":", 1)[1]], cwd=ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
        deadline = time.monotonic() + 20
        with httpx.Client(timeout=1.0) as client:
            while time.monotonic() < deadline:
                try:
                    health = client.get(f"{base_url}/health")
                    ready = client.get(f"{base_url}/ready")
                    if health.status_code == 200 and ready.status_code == 200:
                        return True, True
                except httpx.HTTPError:
                    pass
                time.sleep(0.1)
        raise BackupError("runtime_timeout")
    finally:
        _stop(process)


def validate_recovery(backup: str | Path, artifact: str | Path) -> dict[str, Any]:
    backup_path, artifact_path = Path(backup), Path(artifact)
    started = time.monotonic()
    result: dict[str, Any] = {"recovery_validation": "FAIL", "backup_id": None, "validated_at": datetime.now(timezone.utc).isoformat(), "git_head": None, "backup_schema_version": None, "post_restore_schema_version": None, "backup_db_sha256": None, "quick_check": None, "foreign_key_check_rows": None, "audit_chain": None, "collateral_invariants": None, "post_restore_health": False, "post_restore_ready": False, "duration_ms": 0, "error_code": None}
    try:
        if not backup_path.is_file():
            raise BackupError("backup_not_found")
        sidecar = metadata_path(backup_path)
        if not sidecar.is_file():
            raise BackupError("metadata_missing")
        try:
            metadata = json.loads(sidecar.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            raise BackupError("metadata_invalid") from None
        if not isinstance(metadata, dict) or not {"schema_version", "schema_hash", "table_counts", "audit_final_hash"} <= set(metadata):
            raise BackupError("metadata_invalid")
        result.update({"backup_id": metadata.get("backup_id"), "git_head": metadata.get("git_head"), "backup_schema_version": metadata.get("schema_version"), "backup_db_sha256": metadata.get("backup_db_sha256")})
        if metadata.get("backup_db_sha256") is not None and _sha256(backup_path) != metadata["backup_db_sha256"]:
            raise BackupError("backup_hash_mismatch")
        if int(metadata["schema_version"]) > CURRENT_SCHEMA_VERSION:
            raise BackupError("schema_unsupported")
        with tempfile.TemporaryDirectory(prefix="demo-prediction-recovery-") as directory:
            restored = Path(directory) / "restored.sqlite3"
            restore_backup(backup_path, restored, production_db=Path(directory) / "production.sqlite3")
            conn = connect(str(restored))
            try:
                quick = conn.execute("pragma quick_check").fetchone()[0]
                if quick != "ok":
                    raise BackupError("sqlite_quick_check_failed")
                fk_rows = len(conn.execute("pragma foreign_key_check").fetchall())
                if fk_rows:
                    raise BackupError("foreign_key_check_failed")
                version = int(conn.execute("pragma user_version").fetchone()[0])
                if version > CURRENT_SCHEMA_VERSION:
                    raise BackupError("schema_unsupported")
                tables = {row[0] for row in conn.execute("select name from sqlite_master where type='table'")}
                if CURRENT_SCHEMA_REQUIRED_TABLES.difference(tables):
                    raise BackupError("required_table_missing")
                audit = verify_audit_chain(conn)["integrity_status"]
                if audit not in {"empty", "verified"}:
                    raise BackupError("audit_chain_failed")
                collateral = verify_collateral_invariants(conn)["integrity_status"]
                if collateral != "verified":
                    raise BackupError("collateral_invariant_failed")
                result.update({"post_restore_schema_version": version, "quick_check": quick, "foreign_key_check_rows": fk_rows, "audit_chain": audit, "collateral_invariants": collateral})
            finally:
                conn.close()
            health, ready = _runtime(f"http://127.0.0.1:{_port()}", restored)
            if not health:
                raise BackupError("health_failed")
            if not ready:
                raise BackupError("ready_failed")
            result.update({"post_restore_health": health, "post_restore_ready": ready, "recovery_validation": "PASS"})
    except BackupError as exc:
        result["error_code"] = exc.code
    except Exception:
        result["error_code"] = "restore_failed"
    finally:
        result["duration_ms"] = int((time.monotonic() - started) * 1000)
        artifact_path.parent.mkdir(parents=True, exist_ok=True)
        _atomic_json(artifact_path, result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--backup", required=True)
    parser.add_argument("--artifact", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = validate_recovery(args.backup, args.artifact)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else f"recovery_validation={result['recovery_validation']} error_code={result['error_code']}")
    return 0 if result["recovery_validation"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
