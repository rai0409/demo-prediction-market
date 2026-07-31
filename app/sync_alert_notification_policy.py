from __future__ import annotations
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
import fcntl, hashlib, json, os
from pathlib import Path
from app.sync_alert import evaluate_sync_health

SCHEMA_VERSION=1
INTERVALS={"warning":timedelta(hours=24),"critical":timedelta(hours=6),"check_error":timedelta(hours=1)}
def state_path(db_path:str)->Path:return Path(db_path).resolve().with_name(Path(db_path).resolve().name+".sync-alert-state.json")
@contextmanager
def _lock(path):
 f=open(str(path)+".lock","a")
 try:
  try: fcntl.flock(f,fcntl.LOCK_EX|fcntl.LOCK_NB)
  except BlockingIOError: yield False; return
  yield True
 finally: fcntl.flock(f,fcntl.LOCK_UN); f.close()
def _load(path):
 if not path.exists(): return {"schema_version":SCHEMA_VERSION}
 try:
  value=json.loads(path.read_text())
  if value.get("schema_version")!=SCHEMA_VERSION: raise ValueError
  return value
 except Exception: raise ValueError("state_error")
def _save(path,value):
 path.parent.mkdir(parents=True,exist_ok=True); tmp=path.with_name("."+path.name+".tmp")
 tmp.write_text(json.dumps(value,sort_keys=True)); os.chmod(tmp,0o600); os.replace(tmp,path); os.chmod(path,0o600)
def decide(conn,db_path,now=None):
 now=now or datetime.now(timezone.utc); path=state_path(db_path)
 with _lock(path) as ok:
  if not ok:return {"status":"decision_busy","notify":False,"action":"none","error_code":"decision_busy"}
  try: old=_load(path); result=evaluate_sync_health(conn,now=now)
  except Exception:return {"status":"error","notify":False,"action":"none","error_code":"state_error"}
  state=result["state"]; prev=old.get("last_observed_state"); action="none"; reminder=False
  if state in {"warning","critical","check_error"}:
   action="notify_"+state; last=old.get("last_notification_at")
   if prev==state and last:
    try: reminder=now-datetime.fromisoformat(last)>=INTERVALS[state]
    except ValueError:return {"status":"error","notify":False,"action":"none","error_code":"state_error"}
    if not reminder: action="none"
  elif state=="healthy" and prev in {"warning","critical","check_error"}: action="notify_recovery"
  did=hashlib.sha256(f"{state}:{action}:{now.isoformat()}".encode()).hexdigest()[:16] if action!="none" else None
  old.update({"schema_version":SCHEMA_VERSION,"last_observed_state":state,"last_observed_reason_code":result["reason_code"],"last_evaluated_at":now.isoformat(),"pending_decision_id":did,"pending_action":action})
  _save(path,old); return {**result,"status":"ok","notify":action!="none","action":action,"is_reminder":reminder,"decision_id":did,"last_notification_at":old.get("last_notification_at"),"error_code":None}
def acknowledge(db_path,decision_id):
 path=state_path(db_path)
 with _lock(path) as ok:
  if not ok:return {"status":"decision_busy","error_code":"decision_busy"}
  try:s=_load(path)
  except ValueError:return {"status":"error","error_code":"state_error"}
  if not decision_id or s.get("pending_decision_id")!=decision_id:return {"status":"error","error_code":"invalid_decision"}
  s["last_notification_at"]=datetime.now(timezone.utc).isoformat();s["last_notification_state"]=s.get("last_observed_state");s["last_notification_action"]=s.get("pending_action");s.pop("pending_decision_id",None);_save(path,s);return {"status":"ok","error_code":None}
