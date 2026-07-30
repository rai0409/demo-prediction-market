from pathlib import Path


def test_systemd_service_is_hardened_web_only_template():
    unit = Path("deploy/systemd/demo-prediction-market.service").read_text()
    assert "ExecStart=/home/rai/demo-prediction-market/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8093" in unit
    assert "Environment=DEMO_PREDICTION_LIVE=0" in unit
    for directive in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ReadWritePaths=/home/rai/demo-prediction-market/data"):
        assert directive in unit
    for forbidden in (".timer", "sync_polymarket", "backup_database", "restore_database", "ExecStartPre", "0.0.0.0"):
        assert forbidden not in unit
