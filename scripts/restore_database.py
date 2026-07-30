from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0, str(ROOT))
from app.database_backup import BackupError, restore_backup
from app.config import get_settings
def main(argv=None):
    p=argparse.ArgumentParser(); p.add_argument('--backup', required=True); p.add_argument('--output', required=True); p.add_argument('--overwrite', action='store_true'); p.add_argument('--json', action='store_true'); a=p.parse_args(argv)
    try: result=restore_backup(a.backup, a.output, production_db=get_settings().db_path, overwrite=a.overwrite)
    except BackupError as exc: result={"status":"failed","error_code":exc.code}; code=2
    else: code=0
    print(json.dumps(result, ensure_ascii=False, sort_keys=True) if a.json else f"status={result['status']} error_code={result.get('error_code')}")
    return code
if __name__ == '__main__': raise SystemExit(main())
