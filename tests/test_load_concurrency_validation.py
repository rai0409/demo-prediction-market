import importlib.util
import json
from pathlib import Path
import subprocess
import sys


SCRIPT_PATH = Path(__file__).parents[1] / "scripts" / "validate_load_concurrency.py"
SPEC = importlib.util.spec_from_file_location("load_validation", SCRIPT_PATH)
load_validation = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(load_validation)


def test_percentile_handles_empty_small_and_normal_samples():
    assert load_validation.percentile([], 0.95) is None
    assert load_validation.percentile([4], 0.50) == 4
    assert load_validation.percentile([1, 3], 0.95) == 3
    assert load_validation.percentile([1, 2, 3, 4, 5], 0.50) == 3
    assert load_validation.percentile([1, 2, 3, 4, 5], 0.95) == 5


def test_metric_summary_and_safe_envelope_classification():
    summary = load_validation.metric_summary([1.0, 2.0, 3.0], [200, 200, 500], 0.0)
    assert summary["request_count"] == 3
    assert summary["success_count"] == 2
    assert summary["unexpected_status_count"] == 1
    levels = {
        "1": {"unexpected_error_count": 0, "timeout_count": 0, "connection_error_count": 0, "p95_ms": 10, "p99_ms": 20},
        "2": {"unexpected_error_count": 0, "timeout_count": 0, "connection_error_count": 0, "p95_ms": 20, "p99_ms": 30},
        "4": {"unexpected_error_count": 1, "timeout_count": 0, "connection_error_count": 0, "sqlite_locked_error_count": 0, "p95_ms": 1, "p99_ms": 1},
    }
    assert load_validation.safe_envelope(levels, p95_limit=25, p99_limit=40) == 2
    levels["4"]["unexpected_error_count"] = 0
    levels["4"]["sqlite_locked_error_count"] = 1
    assert load_validation.safe_envelope(levels, p95_limit=25, p99_limit=40) == 2
    levels["4"]["sqlite_locked_error_count"] = 0
    levels["2"]["p95_ms"] = 26
    assert load_validation.safe_envelope(levels, p95_limit=25, p99_limit=40) == 1


class _FakeClient:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()

    def close(self):
        pass


def test_persistent_read_client_reuse_is_bounded_below_request_count(monkeypatch):
    constructed = []
    monkeypatch.setattr(load_validation, "create_http_client", lambda *_args: constructed.append(_FakeClient()) or constructed[-1])
    monkeypatch.setattr(load_validation, "request_once", lambda *_args, **_kwargs: (200, 1.0, None, {}))
    result = load_validation.run_read_level("http://example.test", 4, 100)
    assert result["client_construction_count"] == 1
    assert len(constructed) == 1
    assert result["request_count"] == 100


def test_persistent_write_clients_are_participant_isolated_and_bounded(monkeypatch):
    constructed = []
    participants = [{"participant_id": f"p-{index}", "token": f"token-{index}"} for index in range(4)]
    monkeypatch.setattr(load_validation, "create_http_client", lambda *_args: constructed.append(_FakeClient()) or constructed[-1])
    monkeypatch.setattr(load_validation, "request_once", lambda *_args, **_kwargs: (200, 1.0, None, {"reservation_id": 1}))
    result = load_validation.run_write_level("http://example.test", "market", participants, 4, "write")
    assert result["client_construction_count"] == 4
    assert len(constructed) == 4
    assert len({id(client) for client in constructed}) == len(participants)
    assert result["request_count"] == 8


def test_failure_classification_and_secret_free_metadata(tmp_path):
    db_path = tmp_path / "isolated.sqlite3"
    load_validation.prepare_database(db_path, participant_count=1)
    metadata = load_validation.environment_metadata(db_path)
    assert {"username", "hostname", "ip", "home", "secret", "token", "password"}.isdisjoint(metadata)
    checks = load_validation.integrity(db_path)
    scenarios = {"read": {"1": {"unexpected_status_count": 0, "timeout_count": 0, "connection_error_count": 0, "p95_ms": 1, "p99_ms": 1}}, "write": {}, "mixed": {}}
    assert load_validation.failure_codes(scenarios, checks) == []


def test_process_cleanup_helper_terminates_child():
    process = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"])
    load_validation.terminate_process(process)
    assert process.poll() is not None


def test_cli_smoke_uses_v2_methodology_and_temporary_database(tmp_path):
    output = tmp_path / "load-artifact.json"
    completed = subprocess.run(
        [sys.executable, str(SCRIPT_PATH), "--mode", "smoke", "--output", str(output)],
        cwd=SCRIPT_PATH.parents[1],
        check=False,
        capture_output=True,
        text=True,
        timeout=120,
    )
    assert completed.returncode in {0, 2}, completed.stderr
    artifact = json.loads(output.read_text())
    assert artifact["schema_version"] == 2
    assert artifact["methodology_version"] == 2
    assert artifact["mode"] == "smoke"
    assert artifact["commercial_load_readiness"] in {"PASS", "FAIL"}
    assert artifact["methodology"] == {
        "persistent_http_client": True,
        "http_keepalive": True,
        "client_scope": "scenario_level",
        "write_client_scope": "participant_level",
        "participant_cookie_isolation": True,
        "database_isolation": "fresh_per_level",
        "database_growth_confounded": False,
        "single_process_uvicorn": True,
        "access_log": "enabled",
        "subprocess_output": "DEVNULL",
    }
    assert artifact["integrity"]["scenario_levels"]["write"]["1"]["quick_check"] == "ok"
    assert artifact["integrity"]["scenario_levels"]["write"]["2"]["quick_check"] == "ok"
    assert artifact["comparison_to_v1"]["v1_read_safe_concurrency"] == 8
    assert artifact["comparison_to_v1"]["v1_write_safe_concurrency"] == 16
    assert artifact["comparison_to_v1"]["v1_mixed_safe_concurrency"] == 32
    assert "demo_prediction.sqlite3" not in json.dumps(artifact)


def test_historical_artifact_hashes_are_fixed():
    root = SCRIPT_PATH.parents[1]
    import hashlib
    assert hashlib.sha256((root / "runtime/load-concurrency-validation.json").read_bytes()).hexdigest() == load_validation.SOURCE_V1_ARTIFACT_SHA256
    assert hashlib.sha256((root / "runtime/load-latency-diagnosis.json").read_bytes()).hexdigest() == load_validation.DIAGNOSIS_ARTIFACT_SHA256
