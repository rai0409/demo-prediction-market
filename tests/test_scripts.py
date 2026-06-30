import os
import subprocess
import sys


def test_check_live_fetch_script_works_in_sample_mode():
    env = os.environ.copy()
    env["DEMO_PREDICTION_LIVE"] = "0"
    result = subprocess.run(
        [sys.executable, "scripts/check_live_fetch.py"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )
    assert result.returncode == 0
    assert "live_enabled=False" in result.stdout
    assert "fetch_status=sample_fallback" in result.stdout
    assert "normalized_market_count=" in result.stdout
