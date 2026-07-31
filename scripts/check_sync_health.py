from __future__ import annotations
import argparse, json, sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path: sys.path.insert(0,str(ROOT))
from app.config import get_settings
from app.storage import connect
from app.sync_alert import evaluate_sync_health, exit_code
def main(argv=None):
 p=argparse.ArgumentParser(); p.add_argument("--json",action="store_true"); a=p.parse_args(argv)
 try:
  conn=connect(get_settings().db_path); result=evaluate_sync_health(conn); conn.close()
 except Exception:
  result={"state":"check_error","severity":"critical","freshness_status":"unavailable","last_sync_success_at":None,"last_sync_status":None,"consecutive_failures":0,"reason_code":"sync_check_error","evaluated_at":""}
 print(json.dumps(result,ensure_ascii=False,sort_keys=True) if a.json else f"state={result['state']} severity={result['severity']}")
 return exit_code(result)
if __name__=="__main__": raise SystemExit(main())
