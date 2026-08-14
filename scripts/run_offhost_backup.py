from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.offhost_backup import OffhostConfig, run_offhost_replication


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Replicate verified local backups to Amazon S3")
    parser.add_argument("--directory", required=True)
    parser.add_argument("--state-directory", required=True)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    result = run_offhost_replication(args.directory, args.state_directory, OffhostConfig.from_env())
    if args.json:
        print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    else:
        print(f"off-host replication: {result['status']}")
    return 0 if result["status"] == "PASS" else 2


if __name__ == "__main__":
    raise SystemExit(main())
