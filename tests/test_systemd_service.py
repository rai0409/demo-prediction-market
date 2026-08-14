from pathlib import Path


DATA_PATH = "/home/rai/demo-prediction-market/data"
RUNTIME_PATH = "/home/rai/demo-prediction-market/runtime"


def _read_write_paths(unit: str) -> set[str]:
    return {line.removeprefix("ReadWritePaths=") for line in unit.splitlines() if line.startswith("ReadWritePaths=")}


def test_systemd_service_is_hardened_web_only_template():
    unit = Path("deploy/systemd/demo-prediction-market.service").read_text()
    assert "ExecStart=/home/rai/demo-prediction-market/.venv/bin/python -m uvicorn app.main:app --host 127.0.0.1 --port 8093" in unit
    assert "Environment=DEMO_PREDICTION_LIVE=0" in unit
    assert _read_write_paths(unit) == {DATA_PATH, RUNTIME_PATH}
    for directive in ("NoNewPrivileges=true", "PrivateTmp=true", "PrivateDevices=true", "ProtectSystem=strict", "ProtectHome=read-only"):
        assert directive in unit
    for forbidden in (".timer", "sync_polymarket", "backup_database", "restore_database", "ExecStartPre", "0.0.0.0"):
        assert forbidden not in unit


def test_sync_oneshot_service_and_timer_are_bounded_and_locked_by_cli():
    service = Path("deploy/systemd/demo-prediction-market-sync.service").read_text()
    timer = Path("deploy/systemd/demo-prediction-market-sync.timer").read_text()
    assert "Type=oneshot" in service
    assert "sync_polymarket_markets.py --json" in service
    assert "Environment=DEMO_PREDICTION_LIVE=0" in service
    assert _read_write_paths(service) == {DATA_PATH, RUNTIME_PATH}
    assert "ProtectHome=read-only" in service
    assert "Restart=" not in service and "bash -c" not in service
    assert "OnActiveSec=2min" in timer and "OnUnitInactiveSec=5min" in timer
    assert "OnBootSec" not in timer and "OnUnitActiveSec" not in timer
    assert "Persistent" not in timer and "AccuracySec=30s" in timer
    assert "Unit=demo-prediction-market-sync.service" in timer and "WantedBy=timers.target" in timer


def test_sync_alert_units_deliver_webhook_notifications_without_masking_failures():
    service = Path("deploy/systemd/demo-prediction-market-sync-alert.service").read_text()
    timer = Path("deploy/systemd/demo-prediction-market-sync-alert.timer").read_text()
    assert "Type=oneshot" in service
    assert "run_sync_alert_notification.py --json" in service
    assert "check_sync_health.py" not in service
    assert "EnvironmentFile=/home/rai/demo-prediction-market/.env" in service
    assert "Environment=DEMO_PREDICTION_LIVE=0" in service
    assert "SuccessExitStatus" not in service and "Restart=" not in service
    assert _read_write_paths(service) == {DATA_PATH}
    assert "ProtectHome=read-only" in service
    for secret_marker in ("DEMO_SYNC_ALERT_WEBHOOK_URL=", "token=", "credential=", "api_key="):
        assert secret_marker not in service.lower()
    assert "OnBootSec=4min" in timer and "OnUnitActiveSec=5min" in timer
    assert "AccuracySec=30s" in timer and "Persistent=true" in timer
    assert "Unit=demo-prediction-market-sync-alert.service" in timer
    assert Path("scripts/check_sync_health.py").is_file()
    assert Path("scripts/run_sync_alert_notification.py").is_file()


def test_runtime_directory_is_present_without_tracking_generated_files():
    gitignore = Path(".gitignore").read_text()
    assert Path("runtime/.gitkeep").is_file()
    assert "runtime/*" in gitignore
    assert "!runtime/.gitkeep" in gitignore

def test_backup_oneshot_and_daily_timer_are_hardened():
    service=Path("deploy/systemd/demo-prediction-market-backup.service").read_text(); timer=Path("deploy/systemd/demo-prediction-market-backup.timer").read_text()
    assert "Type=oneshot" in service and "run_scheduled_backup.py --directory /home/rai/demo-prediction-market/runtime/backups --daily-retention 7 --weekly-retention 4 --offhost-receipts-directory /home/rai/demo-prediction-market/runtime/offhost-backups/receipts --json" in service
    assert _read_write_paths(service)=={DATA_PATH,RUNTIME_PATH}
    for item in ("EnvironmentFile=/home/rai/demo-prediction-market/.env", "Environment=DEMO_PREDICTION_LIVE=0", "TimeoutStartSec=300", "StandardOutput=journal", "StandardError=journal", "SyslogIdentifier=demo-prediction-market-backup", "PrivateNetwork=true", "ProtectHome=read-only", "NoNewPrivileges=true", "PrivateTmp=true", "PrivateDevices=true", "ProtectSystem=strict", "ProtectKernelTunables=true", "ProtectKernelModules=true", "ProtectControlGroups=true", "RestrictSUIDSGID=true", "LockPersonality=true", "RestrictRealtime=true", "SystemCallArchitectures=native", "UMask=0077"):
        assert item in service
    assert "Restart=" not in service and "SuccessExitStatus=" not in service and "bash -c" not in service
    for item in ("OnCalendar=*-*-* 03:15:00","Persistent=true","RandomizedDelaySec=15min","Unit=demo-prediction-market-backup.service","WantedBy=timers.target"): assert item in timer


def test_offhost_backup_unit_has_network_only_in_its_independent_service():
    local = Path("deploy/systemd/demo-prediction-market-backup.service").read_text()
    service = Path("deploy/systemd/demo-prediction-market-offhost-backup.service").read_text()
    timer = Path("deploy/systemd/demo-prediction-market-offhost-backup.timer").read_text()
    assert "PrivateNetwork=true" in local
    assert "OnSuccess=demo-prediction-market-offhost-backup.service" in local
    assert "Wants=network-online.target" in service and "After=network-online.target" in service
    assert "PrivateNetwork=true" not in service
    assert _read_write_paths(service) == {RUNTIME_PATH}
    for item in ("Type=oneshot", "run_offhost_backup.py", "UMask=0077", "NoNewPrivileges=true", "ProtectHome=read-only"):
        assert item in service
    assert "Restart=" not in service and "SuccessExitStatus=" not in service and "bash -c" not in service
    for item in ("OnBootSec=10min", "OnUnitInactiveSec=1h", "Persistent=true", "RandomizedDelaySec=10min", "Unit=demo-prediction-market-offhost-backup.service", "WantedBy=timers.target"):
        assert item in timer
