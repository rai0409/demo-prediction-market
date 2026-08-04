from datetime import datetime, timedelta, timezone
from app.sync_alert import evaluate_sync_health, exit_code

def add(conn,status,success=None):
 now=datetime.now(timezone.utc).isoformat(); conn.execute("insert into market_sync_runs(provider,attempted_at,successful_at,status,error_code,requested,received,valid,inserted,updated,unchanged,skipped,failed,error_counts_json) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",("x",now,success,status,None,0,0,0,0,0,0,0,0,"{}")); conn.commit()
def test_states(db_conn):
 assert evaluate_sync_health(db_conn)["state"]=="not_initialized"
 now=datetime.now(timezone.utc).isoformat(); add(db_conn,"success",now)
 assert evaluate_sync_health(db_conn)["state"]=="healthy"
 for _ in range(3): add(db_conn,"upstream_error")
 assert exit_code(evaluate_sync_health(db_conn))==1
 for _ in range(3): add(db_conn,"upstream_error")
 assert exit_code(evaluate_sync_health(db_conn))==2
def test_dry_run_and_lock_are_not_failures(db_conn):
 add(db_conn,"success",datetime.now(timezone.utc).isoformat()); add(db_conn,"dry_run"); add(db_conn,"sync_already_running")
 assert evaluate_sync_health(db_conn)["consecutive_failures"]==0
