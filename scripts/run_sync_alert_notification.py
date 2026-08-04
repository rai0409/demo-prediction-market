from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.storage import connect
from app.sync_alert_delivery import EXIT_CONFIGURATION, run


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args(argv)
    try:
        settings = get_settings()
        conn = connect(settings.db_path)
    except Exception:
        code, result = EXIT_CONFIGURATION, {
            "status": "error", "notify": False, "delivered": False, "acknowledged": False,
            "action": None, "state": None, "reason_code": None, "decision_id": None, "error_code": "configuration_error",
        }
    else:
        try:
            code, result = run(conn, settings)
        finally:
            conn.close()
    if args.json:
        print(json.dumps(result, sort_keys=True))
    else:
        print(f"status={result['status']} error_code={result['error_code']}")
    return code


if __name__ == "__main__":
    raise SystemExit(main())
