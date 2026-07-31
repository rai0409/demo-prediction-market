from __future__ import annotations
import argparse,json,sys
from pathlib import Path
ROOT=Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:sys.path.insert(0,str(ROOT))
from app.config import get_settings
from app.storage import connect
from app.sync_alert_notification_policy import acknowledge,decide
def main(argv=None):
 p=argparse.ArgumentParser();p.add_argument('--json',action='store_true');p.add_argument('--acknowledge');a=p.parse_args(argv);settings=get_settings()
 r=acknowledge(settings.db_path,a.acknowledge) if a.acknowledge else decide(connect(settings.db_path),settings.db_path)
 print(json.dumps(r,sort_keys=True));return 4 if r.get('error_code')=='decision_busy' else (3 if r.get('error_code') else 0)
if __name__=='__main__':raise SystemExit(main())
