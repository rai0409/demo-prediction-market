#!/usr/bin/env python3
"""Run the scheduled isolated restore drill against the newest local backup."""
from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.recovery_drill import DEFAULT_ARTIFACT, DEFAULT_BACKUP_DIRECTORY, run_scheduled_restore_drill


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run an isolated scheduled restore drill")
    parser.add_argument("--backup-directory", default=str(DEFAULT_BACKUP_DIRECTORY))
    parser.add_argument("--artifact", default=str(DEFAULT_ARTIFACT))
    parser.add_argument("--json", action="store_true")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    result = run_scheduled_restore_drill(args.backup_directory, artifact=args.artifact)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if args.json else result["drill_status"])
    return 0 if result["drill_status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
