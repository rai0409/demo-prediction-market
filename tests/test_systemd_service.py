from pathlib import Path


def test_systemd_service_is_hardened_web_only_template():
    unit = Path("deploy/systemd/demo-prediction-market.service").read_text()
    assert "ExecStart=/home/rai/demo-prediction-market/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8093" in unit
    assert "Environment=DEMO_PREDICTION_LIVE=0" in unit
    for directive in ("NoNewPrivileges=true", "PrivateTmp=true", "ProtectSystem=strict", "ReadWritePaths=/home/rai/demo-prediction-market/data"):
        assert directive in unit
    for forbidden in (".timer", "sync_polymarket", "backup_database", "restore_database", "ExecStartPre", "0.0.0.0"):
        assert forbidden not in unit


def test_sync_oneshot_service_and_timer_are_bounded_and_locked_by_cli():
    service = Path("deploy/systemd/demo-prediction-market-sync.service").read_text()
    timer = Path("deploy/systemd/demo-prediction-market-sync.timer").read_text()
    assert "Type=oneshot" in service
    assert "sync_polymarket_markets.py --json" in service
    assert "Environment=DEMO_PREDICTION_LIVE=0" in service
    assert "ReadWritePaths=/home/rai/demo-prediction-market/data" in service
    assert "Restart=" not in service and "bash -c" not in service
    assert "OnBootSec=2min" in timer and "OnUnitActiveSec=5min" in timer
    assert "Persistent=true" in timer and "Unit=demo-prediction-market-sync.service" in timer
