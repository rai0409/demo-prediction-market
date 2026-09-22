#!/usr/bin/env python3
"""Evaluate local recovery evidence and publish its health status."""
from __future__ import annotations

import argparse
from datetime import timedelta
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.recovery_health import (
    DEFAULT_ARTIFACT,
    DEFAULT_BACKUP_DIRECTORY,
    DEFAULT_MAX_BACKUP_AGE,
    DEFAULT_MAX_REPLICATION_LAG,
    DEFAULT_OFFHOST_STATE_DIRECTORY,
    check_recovery_health,
)


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check scheduled backup and off-host recovery health")
    parser.add_argument("--backup-directory", default=str(DEFAULT_BACKUP_DIRECTORY))
    parser.add_argument("--offhost-state-directory", default=str(DEFAULT_OFFHOST_STATE_DIRECTORY))
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--max-backup-age-hours", type=float, default=DEFAULT_MAX_BACKUP_AGE.total_seconds() / 3600)
    parser.add_argument("--max-replication-lag-hours", type=float, default=DEFAULT_MAX_REPLICATION_LAG.total_seconds() / 3600)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = check_recovery_health(
        args.backup_directory,
        args.offhost_state_directory,
        artifact=args.artifact,
        max_backup_age=timedelta(hours=args.max_backup_age_hours),
        max_replication_lag=timedelta(hours=args.max_replication_lag_hours),
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else result["status"])
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
