from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.backup_retention import run_scheduled_backup
from app.config import get_settings


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Create and retain scheduled local database backups")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--daily-retention", type=int, default=7)
    parser.add_argument("--weekly-retention", type=int, default=4)
    parser.add_argument("--offhost-receipts-directory", required=True)
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_scheduled_backup(
        get_settings().db_path,
        args.directory,
        daily_retention=args.daily_retention,
        weekly_retention=args.weekly_retention,
        offhost_receipts_directory=args.offhost_receipts_directory,
    )
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"scheduled backup: {result['status']}")
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
