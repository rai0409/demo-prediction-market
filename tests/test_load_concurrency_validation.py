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
        "4": {"unexpected_error_count": 1, "timeout_count": 0, "connection_error_count": 0, "p95_ms": 1, "p99_ms": 1},
    }
    assert load_validation.safe_envelope(levels, p95_limit=25, p99_limit=40) == 2


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


def test_cli_smoke_uses_temporary_database_and_writes_artifact(tmp_path):
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
    assert artifact["schema_version"] == 1
    assert artifact["mode"] == "smoke"
    assert artifact["commercial_load_readiness"] in {"PASS", "FAIL"}
    assert artifact["environment"]["schema_version"] == 1
    assert artifact["integrity"]["quick_check"] == "ok"
    assert "demo_prediction.sqlite3" not in json.dumps(artifact)
