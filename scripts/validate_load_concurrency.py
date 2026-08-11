#!/usr/bin/env python3
"""Measure the current single-process Uvicorn + SQLite concurrency envelope.

This is deliberately a validation harness, not a load-tuning tool.  It only uses a
temporary SQLite database and the checked-in offline sample market data.
"""
from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
import os
from pathlib import Path
import platform
import socket
import sqlite3
import subprocess
import sys
import tempfile
import time
from typing import Any

import httpx

REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from app.collateral_ledger import (
    CollateralLedgerError,
    POINT_SCALE,
    allocate_v2_points_to_participant,
    bootstrap_v2_point_supply,
    cancel_v2_order_collateral,
    create_collateral_market,
    reject_v2_order_collateral,
    reserve_v2_order_collateral,
    verify_collateral_invariants,
)
from app.polymarket_gamma import load_markets
from app.storage import (
    CURRENT_SCHEMA_VERSION,
    connect,
    create_user_account,
    create_user_session,
    ensure_demo_user,
    init_db,
    store_markets,
    verify_audit_chain,
)


READ_LEVELS = [1, 2, 4, 8, 16, 32]
MIXED_LEVELS = [8, 16, 32]
REQUEST_TIMEOUT_SECONDS = 10.0


def percentile(values: list[float], fraction: float) -> float | None:
    if not values:
        return None
    ordered = sorted(values)
    index = max(0, min(len(ordered) - 1, int((len(ordered) - 1) * fraction + 0.999999)))
    return round(ordered[index], 3)


def metric_summary(samples_ms: list[float], statuses: list[int], started_at: float) -> dict[str, Any]:
    count = len(samples_ms)
    elapsed = max(time.perf_counter() - started_at, 0.000001)
    return {
        "request_count": count,
        "success_count": sum(status < 400 for status in statuses),
        "unexpected_status_count": sum(status >= 400 for status in statuses),
        "timeout_count": 0,
        "connection_error_count": 0,
        "p50_ms": percentile(samples_ms, 0.50),
        "p95_ms": percentile(samples_ms, 0.95),
        "p99_ms": percentile(samples_ms, 0.99),
        "max_ms": round(max(samples_ms), 3) if samples_ms else None,
        "requests_per_second": round(count / elapsed, 3),
    }


def safe_envelope(level_results: dict[str, dict[str, Any]], *, p95_limit: float, p99_limit: float) -> int | None:
    safe: int | None = None
    for level_text, result in level_results.items():
        level = int(level_text)
        if (
            result.get("unexpected_error_count", result.get("unexpected_status_count", 0)) == 0
            and result.get("timeout_count", 0) == 0
            and result.get("connection_error_count", 0) == 0
            and (result.get("p95_ms") or float("inf")) <= p95_limit
            and (result.get("p99_ms") or float("inf")) <= p99_limit
        ):
            safe = max(safe or level, level)
    return safe


def select_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def terminate_process(process: subprocess.Popen[str] | None) -> None:
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=10)


def environment_metadata(db_path: Path) -> dict[str, Any]:
    conn = connect(str(db_path))
    try:
        return {
            "python_version": platform.python_version(),
            "sqlite_version": sqlite3.sqlite_version,
            "platform": platform.system(),
            "logical_cpu_count": os.cpu_count(),
            "git_head": git_head(),
            "schema_version": int(conn.execute("pragma user_version").fetchone()[0]),
            "busy_timeout_ms": int(conn.execute("pragma busy_timeout").fetchone()[0]),
            "uvicorn_mode": "single_process",
        }
    finally:
        conn.close()


def git_head() -> str:
    result = subprocess.run(["git", "rev-parse", "HEAD"], check=True, capture_output=True, text=True)
    return result.stdout.strip()


def prepare_database(db_path: Path, participant_count: int) -> tuple[str, list[dict[str, str]]]:
    conn = connect(str(db_path))
    try:
        init_db(conn)
        markets = load_markets(live=False, limit=50)
        store_markets(conn, markets)
        market_id = markets[0]["market_id"]
        create_collateral_market(conn, market_id=market_id)
        bootstrap_v2_point_supply(conn, amount_micro=(participant_count + 4) * POINT_SCALE, idempotency_key="load-bootstrap")
        clients: list[dict[str, str]] = []
        for index in range(participant_count):
            participant_id = f"load-participant-{index}"
            ensure_demo_user(conn, participant_id)
            account = create_user_account(
                conn,
                email=f"load-{index}@example.test",
                password="load validation password",
                participant_id=participant_id,
            )
            _, token = create_user_session(conn, user_id=account["id"], ttl_seconds=3600)
            conn.commit()
            allocate_v2_points_to_participant(
                conn,
                participant_id=participant_id,
                amount_micro=POINT_SCALE,
                idempotency_key=f"load-allocation-{index}",
            )
            clients.append({"participant_id": participant_id, "token": token})
        return market_id, clients
    finally:
        conn.close()


def wait_ready(base_url: str) -> None:
    deadline = time.monotonic() + 20
    with httpx.Client(timeout=1.0) as client:
        while time.monotonic() < deadline:
            try:
                if client.get(f"{base_url}/health").status_code == 200 and client.get(f"{base_url}/ready").status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            time.sleep(0.1)
    raise RuntimeError("server readiness failed")


def request_once(base_url: str, method: str, path: str, *, headers: dict[str, str] | None = None, payload: dict[str, Any] | None = None) -> tuple[int | None, float, str | None]:
    started = time.perf_counter()
    try:
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as client:
            response = client.request(method, f"{base_url}{path}", headers=headers, json=payload)
        return response.status_code, (time.perf_counter() - started) * 1000, None
    except httpx.TimeoutException:
        return None, (time.perf_counter() - started) * 1000, "timeout"
    except httpx.HTTPError:
        return None, (time.perf_counter() - started) * 1000, "connection"


def run_read_level(base_url: str, level: int, samples: int) -> dict[str, Any]:
    paths = ["/health", "/ready", "/api/markets"]
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=level) as pool:
        outcomes = list(pool.map(lambda index: request_once(base_url, "GET", paths[index % len(paths)]), range(samples)))
    latencies = [outcome[1] for outcome in outcomes]
    statuses = [outcome[0] or 599 for outcome in outcomes]
    result = metric_summary(latencies, statuses, started)
    result["timeout_count"] = sum(outcome[2] == "timeout" for outcome in outcomes)
    result["connection_error_count"] = sum(outcome[2] == "connection" for outcome in outcomes)
    result["unexpected_status_count"] = sum(status != 200 for status in statuses)
    result["success_count"] = sum(status == 200 for status in statuses)
    return result


def auth_headers(token: str, request_id: str) -> dict[str, str]:
    csrf = f"load-csrf-{request_id}"
    return {"x-csrf-token": csrf, "x-request-id": request_id, "cookie": f"auth_session={token}; demo_csrf={csrf}"}


def reserve_and_cancel(base_url: str, market_id: str, client: dict[str, str], key_prefix: str) -> list[tuple[int | None, float, str | None]]:
    headers = auth_headers(client["token"], key_prefix)
    reserve_payload = {
        "market_id": market_id, "side": "BUY", "outcome": "YES", "quantity": 1,
        "limit_price_micro": POINT_SCALE, "idempotency_key": f"{key_prefix}-reserve",
    }
    reserve = request_once(base_url, "POST", "/api/v2/order-collateral/reservations", headers=headers, payload=reserve_payload)
    outcomes = [reserve]
    if reserve[0] == 200:
        # Fetch the id from the normal public response only; no database shortcut.
        with httpx.Client(timeout=REQUEST_TIMEOUT_SECONDS) as http_client:
            response = http_client.post(f"{base_url}/api/v2/order-collateral/reservations", headers=headers, json=reserve_payload)
        reservation_id = response.json()["reservation_id"]
        cancel_payload = {"idempotency_key": f"{key_prefix}-cancel"}
        outcomes.append(request_once(base_url, "POST", f"/api/v2/order-collateral/reservations/{reservation_id}/cancel", headers=headers, payload=cancel_payload))
    return outcomes


def run_write_level(base_url: str, market_id: str, clients: list[dict[str, str]], level: int, prefix: str) -> dict[str, Any]:
    started = time.perf_counter()
    with ThreadPoolExecutor(max_workers=level) as pool:
        waves = list(pool.map(lambda pair: reserve_and_cancel(base_url, market_id, pair[1], f"{prefix}-{pair[0]}"), enumerate(clients[:level])))
    outcomes = [item for wave in waves for item in wave]
    latencies = [item[1] for item in outcomes]
    statuses = [item[0] or 599 for item in outcomes]
    result = metric_summary(latencies, statuses, started)
    result.update({
        "reserve_count": level,
        "cancel_count": sum(len(wave) == 2 for wave in waves),
        "success_count": sum(status == 200 for status in statuses),
        "expected_rejection_count": 0,
        "unexpected_error_count": sum(status != 200 for status in statuses),
        "unexpected_409_count": sum(status == 409 for status in statuses),
        "unexpected_429_count": sum(status == 429 for status in statuses),
        "timeout_count": sum(item[2] == "timeout" for item in outcomes),
        "connection_error_count": sum(item[2] == "connection" for item in outcomes),
        "operations_per_second": round(len(outcomes) / max(time.perf_counter() - started, 0.000001), 3),
    })
    return result


def run_mixed_level(base_url: str, market_id: str, clients: list[dict[str, str]], level: int, prefix: str) -> dict[str, Any]:
    write_count = max(1, level // 4)
    started = time.perf_counter()
    def operation(index: int):
        if index < write_count:
            return reserve_and_cancel(base_url, market_id, clients[index], f"{prefix}-{index}")
        return [request_once(base_url, "GET", "/api/markets" if index % 2 else "/ready")]
    with ThreadPoolExecutor(max_workers=level) as pool:
        waves = list(pool.map(operation, range(level)))
    outcomes = [item for wave in waves for item in wave]
    latencies = [item[1] for item in outcomes]
    statuses = [item[0] or 599 for item in outcomes]
    result = metric_summary(latencies, statuses, started)
    result.update({
        "reserve_count": write_count,
        "cancel_count": sum(len(wave) == 2 for wave in waves[:write_count]),
        "unexpected_error_count": sum(status != 200 for status in statuses),
        "unexpected_409_count": sum(status == 409 for status in statuses),
        "unexpected_429_count": sum(status == 429 for status in statuses),
        "timeout_count": sum(item[2] == "timeout" for item in outcomes),
        "connection_error_count": sum(item[2] == "connection" for item in outcomes),
        "operations_per_second": round(len(outcomes) / max(time.perf_counter() - started, 0.000001), 3),
    })
    return result


def _concurrent_call(concurrency: int, callback):
    with ThreadPoolExecutor(max_workers=concurrency) as pool:
        return list(pool.map(lambda _: callback(), range(concurrency)))


def direct_contention(db_path: Path, market_id: str) -> dict[str, Any]:
    conn = connect(str(db_path))
    participant = "contention-participant"
    try:
        ensure_demo_user(conn, participant)
        conn.commit()
        allocate_v2_points_to_participant(conn, participant_id=participant, amount_micro=POINT_SCALE, idempotency_key="contention-allocation")
    finally:
        conn.close()
    results: dict[str, Any] = {}
    for level in [2, 4, 8, 16]:
        def reserve():
            worker = connect(str(db_path))
            try:
                return reserve_v2_order_collateral(worker, participant_id=participant, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=POINT_SCALE, idempotency_key=f"contention-{level}-{os.urandom(4).hex()}")["reservation_id"]
            except CollateralLedgerError as exc:
                return exc.code
            finally:
                worker.close()
        values = _concurrent_call(level, reserve)
        success = [value for value in values if isinstance(value, int)]
        allowed = {"insufficient_points", "concurrent_update"}
        results[str(level)] = {"success_count": len(success), "rejections": [value for value in values if not isinstance(value, int)], "passed": len(success) == 1 and all(value in allowed for value in values if not isinstance(value, int))}
        if success:
            release = connect(str(db_path))
            try:
                cancel_v2_order_collateral(release, participant_id=participant, reservation_id=success[0], idempotency_key=f"contention-release-{level}")
            finally:
                release.close()
    # Concurrent release and idempotency each use fresh valid collateral.
    owner = "release-participant"
    conn = connect(str(db_path))
    try:
        ensure_demo_user(conn, owner); conn.commit()
        allocate_v2_points_to_participant(conn, participant_id=owner, amount_micro=POINT_SCALE, idempotency_key="release-allocation")
        reservation = reserve_v2_order_collateral(conn, participant_id=owner, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=POINT_SCALE, idempotency_key="release-reserve")
    finally:
        conn.close()
    reservation_id = reservation["reservation_id"]
    def release(kind: str):
        worker = connect(str(db_path))
        try:
            operation = cancel_v2_order_collateral if kind == "cancel" else reject_v2_order_collateral
            kwargs = {"participant_id": owner} if kind == "cancel" else {}
            return operation(worker, **kwargs, reservation_id=reservation_id, idempotency_key=f"release-{kind}")["release_reason"]
        except CollateralLedgerError as exc:
            return exc.code
        finally:
            worker.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        releases = list(pool.map(release, ("cancel", "reject")))
    release_events = connect(str(db_path))
    try:
        event_count = release_events.execute("select count(*) from order_collateral_events where reservation_id=? and event_type='release'", (reservation_id,)).fetchone()[0]
        ledger_count = release_events.execute("select count(*) from order_collateral_ledger_entries entries join order_collateral_events events on events.id = entries.event_id where entries.reservation_id=? and events.event_type='release'", (reservation_id,)).fetchone()[0]
    finally:
        release_events.close()
    results["concurrent_release"] = {"results": releases, "release_event_count": event_count, "release_ledger_count": ledger_count, "passed": sum(value in {"cancelled", "rejected"} for value in releases) == 1 and event_count == 1 and ledger_count == 2}
    return results


def idempotency_contention(db_path: Path, market_id: str) -> dict[str, Any]:
    participant = "idempotency-participant"
    conn = connect(str(db_path))
    try:
        ensure_demo_user(conn, participant); conn.commit()
        allocate_v2_points_to_participant(conn, participant_id=participant, amount_micro=POINT_SCALE, idempotency_key="idempotency-allocation")
    finally:
        conn.close()
    levels: dict[str, Any] = {}
    for level in [2, 4, 8, 16]:
        key = f"idempotency-{level}"
        def reserve():
            worker = connect(str(db_path))
            try:
                return reserve_v2_order_collateral(worker, participant_id=participant, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=POINT_SCALE, idempotency_key=key)
            except CollateralLedgerError as exc:
                return {"error": exc.code}
            finally:
                worker.close()
        values = _concurrent_call(level, reserve)
        ids = {value["reservation_id"] for value in values if "reservation_id" in value}
        levels[str(level)] = {"reservation_ids": sorted(ids), "passed": len(ids) == 1 and len(values) == level and not any("error" in value for value in values)}
        if ids:
            release = connect(str(db_path))
            try:
                cancel_v2_order_collateral(release, participant_id=participant, reservation_id=next(iter(ids)), idempotency_key=f"{key}-cancel")
            finally:
                release.close()
    conn = connect(str(db_path))
    try:
        try:
            reserve_v2_order_collateral(conn, participant_id=participant, market_id=market_id, side="BUY", outcome="NO", quantity=1, limit_price_micro=POINT_SCALE, idempotency_key="idempotency-2")
            collision = "missing_conflict"
        except CollateralLedgerError as exc:
            collision = exc.code
    finally:
        conn.close()
    return {"levels": levels, "negative_collision": collision, "passed": all(value["passed"] for value in levels.values()) and collision == "idempotency_conflict"}


def integrity(db_path: Path) -> dict[str, Any]:
    conn = connect(str(db_path))
    try:
        quick_check = conn.execute("pragma quick_check").fetchone()[0]
        fk_rows = len(conn.execute("pragma foreign_key_check").fetchall())
        schema_version = int(conn.execute("pragma user_version").fetchone()[0])
        collateral = verify_collateral_invariants(conn)
        audit = verify_audit_chain(conn)
        return {"quick_check": quick_check, "foreign_key_check_rows": fk_rows, "schema_version": schema_version, "collateral": collateral, "audit_chain": audit}
    finally:
        conn.close()


def failure_codes(scenarios: dict[str, Any], checks: dict[str, Any]) -> list[str]:
    codes: list[str] = []
    for kind, p95, p99 in (("read", 500, 1000), ("write", 1000, 3000), ("mixed", 1500, 4000)):
        for result in scenarios.get(kind, {}).values():
            if result.get("timeout_count", 0): codes.append("http_timeout")
            if result.get("connection_error_count", 0): codes.append("connection_error")
            if result.get("unexpected_409_count", 0): codes.append("unexpected_http_409")
            if result.get("unexpected_429_count", 0): codes.append("benchmark_confounded")
            if result.get("unexpected_error_count", result.get("unexpected_status_count", 0)): codes.append("unexpected_http_error")
            if (result.get("p95_ms") or 0) > p95 or (result.get("p99_ms") or 0) > p99: codes.append(f"{kind}_latency_exceeded")
    if checks["quick_check"] != "ok": codes.append("quick_check_failed")
    if checks["foreign_key_check_rows"]: codes.append("foreign_key_violation")
    if checks["schema_version"] != CURRENT_SCHEMA_VERSION: codes.append("schema_version_mismatch")
    if checks["collateral"]["integrity_status"] != "verified": codes.append("collateral_invariant_failed")
    if checks["audit_chain"]["integrity_status"] != "verified": codes.append("audit_chain_failed")
    return sorted(set(codes))


def run(mode: str, output: Path) -> dict[str, Any]:
    levels = [1, 2] if mode == "smoke" else READ_LEVELS
    mixed_levels = [2] if mode == "smoke" else MIXED_LEVELS
    samples = 12 if mode == "smoke" else 100
    output.parent.mkdir(parents=True, exist_ok=True)
    process: subprocess.Popen[str] | None = None
    with tempfile.TemporaryDirectory(prefix="demo-prediction-load-") as temp_dir:
        db_path = Path(temp_dir) / "load-validation.sqlite3"
        market_id, clients = prepare_database(db_path, participant_count=8 if mode == "smoke" else 140)
        port = select_port()
        environment = os.environ.copy() | {
            "DEMO_PREDICTION_DB": str(db_path), "DEMO_PREDICTION_LIVE": "0", "DEMO_PREDICTION_AUTO_REFRESH": "0",
            "DEMO_PREDICTION_WS_ENABLED": "0", "DEMO_TRANSLATION_ENABLED": "0", "DEMO_ADMIN_TOKEN": "load-admin",
        }
        try:
            process = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--host", "127.0.0.1", "--port", str(port)], cwd=REPOSITORY_ROOT, env=environment, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, text=True)
            base_url = f"http://127.0.0.1:{port}"
            wait_ready(base_url)
            read = {str(level): run_read_level(base_url, level, samples) for level in levels}
            write: dict[str, dict[str, Any]] = {}
            offset = 0
            for level in levels:
                write[str(level)] = run_write_level(base_url, market_id, clients[offset:offset + level], level, f"write-{level}")
                offset += level
            mixed: dict[str, dict[str, Any]] = {}
            for level in mixed_levels:
                mixed[str(level)] = run_mixed_level(base_url, market_id, clients[offset:offset + level], level, f"mixed-{level}")
                offset += level
            contention = direct_contention(db_path, market_id)
            idempotency = idempotency_contention(db_path, market_id)
            post_health = request_once(base_url, "GET", "/health")[0] == 200
            post_ready = request_once(base_url, "GET", "/ready")[0] == 200
            checks = integrity(db_path)
        finally:
            terminate_process(process)
        scenarios = {"read": read, "write": write, "mixed": mixed, "same_account_overspend": contention, "idempotency": idempotency}
        codes = failure_codes(scenarios, checks)
        if not contention["concurrent_release"]["passed"]: codes.append("double_release_detected")
        if not idempotency["passed"]: codes.append("idempotency_violation")
        if not post_health or not post_ready: codes.append("post_load_not_ready")
        envelope = {"read": safe_envelope(read, p95_limit=500, p99_limit=1000), "write": safe_envelope(write, p95_limit=1000, p99_limit=3000), "mixed": safe_envelope(mixed, p95_limit=1500, p99_limit=4000)}
        readiness = "PASS" if not codes and all(envelope.values()) else "FAIL"
        artifact = {"schema_version": 1, "git_head": git_head(), "mode": mode, "environment": environment_metadata(db_path), "scenarios": scenarios, "integrity": checks, "safe_envelope": envelope, "commercial_load_readiness": readiness, "failure_codes": sorted(set(codes)), "post_load_health": post_health, "post_load_ready": post_ready}
    output.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n")
    return artifact


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--mode", choices=("smoke", "full"), required=True)
    parser.add_argument("--output", type=Path, default=Path("runtime/load-concurrency-validation.json"))
    args = parser.parse_args()
    try:
        artifact = run(args.mode, args.output)
    except Exception as exc:
        print(f"harness_failure={type(exc).__name__}", file=sys.stderr)
        return 1
    print(json.dumps({"commercial_load_readiness": artifact["commercial_load_readiness"], "safe_envelope": artifact["safe_envelope"], "failure_codes": artifact["failure_codes"]}, indent=2))
    return 0 if artifact["commercial_load_readiness"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
