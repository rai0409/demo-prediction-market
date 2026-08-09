import json
import sqlite3

import pytest

from app.database_backup import BackupError, create_backup, metadata_path, restore_backup
from app.collateral_ledger import POINT_SCALE, allocate_v2_points_to_participant, bootstrap_v2_point_supply, cancel_v2_order_collateral, create_collateral_market, reserve_v2_order_collateral, split_complete_sets, verify_collateral_invariants
from app.storage import DEMO_USER_ID, connect, get_market, init_db, store_markets, verify_audit_chain


def make_db(path, sample_markets):
    conn = connect(str(path))
    init_db(conn)
    store_markets(conn, sample_markets[:1])
    conn.execute("insert into market_sync_runs(provider, attempted_at, successful_at, status, error_code, requested, received, valid, inserted, updated, unchanged, skipped, failed, error_counts_json) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)", ("test", "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00", "success", None, 1, 1, 1, 1, 0, 0, 0, 0, "{}"))
    conn.commit(); conn.close()


def test_backup_restore_round_trip_preserves_storage_readability(tmp_path, sample_markets):
    source, backup, restored = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "restored.db"
    make_db(source, sample_markets)
    result = create_backup(source, backup)
    assert result["status"] == "success"
    assert backup.exists() and metadata_path(backup).exists()
    metadata = json.loads(metadata_path(backup).read_text())
    assert "password_hash" not in json.dumps(metadata)
    restored_result = restore_backup(backup, restored, production_db=source)
    assert restored_result["status"] == "success"
    restored_conn = connect(str(restored))
    assert get_market(restored_conn, sample_markets[0]["market_id"])["market_id"] == sample_markets[0]["market_id"]
    assert restored_conn.execute("pragma integrity_check").fetchone()[0] == "ok"
    assert restored_conn.execute("pragma foreign_key_check").fetchone() is None
    restored_conn.close()


def test_backup_and_restore_reject_unsafe_paths_and_existing_outputs(tmp_path, sample_markets):
    source, backup, output = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "out.db"
    make_db(source, sample_markets)
    with pytest.raises(BackupError, match="same_path"):
        create_backup(source, source)
    create_backup(source, backup)
    with pytest.raises(BackupError, match="output_exists"):
        create_backup(source, backup)
    with pytest.raises(BackupError, match="same_path"):
        restore_backup(backup, backup, production_db=source)
    with pytest.raises(BackupError, match="production_output_forbidden"):
        restore_backup(backup, source, production_db=source)
    output.touch()
    with pytest.raises(BackupError, match="output_exists"):
        restore_backup(backup, output, production_db=source)
    assert restore_backup(backup, output, production_db=source, overwrite=True)["status"] == "success"


def test_restore_rejects_corrupt_backup_and_metadata_mismatch(tmp_path, sample_markets):
    source, backup, output = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "out.db"
    make_db(source, sample_markets)
    create_backup(source, backup)
    metadata_path(backup).write_text('{"schema_hash":"wrong"}')
    with pytest.raises(BackupError, match="metadata_mismatch"):
        restore_backup(backup, output, production_db=source)
    create_backup(source, backup, overwrite=True)
    backup.write_bytes(b"not sqlite")
    with pytest.raises(BackupError):
        restore_backup(backup, output, production_db=source)
    assert not output.exists()


def test_backup_rejects_foreign_key_violations(tmp_path, sample_markets):
    source, backup = tmp_path / "source.db", tmp_path / "backup.sqlite"
    make_db(source, sample_markets)
    conn = sqlite3.connect(source)
    conn.execute("pragma foreign_keys = off")
    conn.execute("insert into user_sessions(id, user_id, token_hash, created_at, expires_at, last_seen_at) values ('s', 'missing', 'h', 'x', 'x', 'x')")
    conn.commit(); conn.close()
    with pytest.raises(BackupError, match="foreign_key_failed"):
        create_backup(source, backup)
    assert not backup.exists()


def test_backup_restore_preserves_v2_collateral_ledger(tmp_path, sample_markets):
    source, backup, restored = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "restored.db"
    make_db(source, sample_markets)
    conn = connect(str(source))
    treasury = bootstrap_v2_point_supply(conn, amount_micro=10_000 * POINT_SCALE, idempotency_key="bootstrap")
    allocation = allocate_v2_points_to_participant(conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="allocation")
    market_id = sample_markets[0]["market_id"]
    create_collateral_market(conn, market_id=market_id)
    split_complete_sets(conn, account_id=treasury["destination_account_id"], market_id=market_id, quantity=250, idempotency_key="split")
    conn.close()
    create_backup(source, backup)
    restore_backup(backup, restored, production_db=source)
    restored_conn = connect(str(restored))
    assert verify_collateral_invariants(restored_conn, market_id=market_id)["integrity_status"] == "verified"
    assert restored_conn.execute("select count(*) from reserve_events").fetchone()[0] == 1
    assert restored_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 1
    assert restored_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 2
    assert restored_conn.execute("select available_micro from point_accounts where account_id = ?", (allocation["destination_account_id"],)).fetchone()[0] == POINT_SCALE
    assert verify_audit_chain(restored_conn)["integrity_status"] == "verified"
    assert restored_conn.execute("select count(*) from demo_users").fetchone()[0] == 1
    restored_conn.close()


def test_backup_restore_preserves_v2_order_collateral_rows_and_locks(tmp_path, sample_markets):
    source, backup, restored = tmp_path / "source.db", tmp_path / "backup.sqlite", tmp_path / "restored.db"
    make_db(source, sample_markets)
    conn = connect(str(source))
    bootstrap_v2_point_supply(conn, amount_micro=3 * POINT_SCALE, idempotency_key="bootstrap-order")
    allocation = allocate_v2_points_to_participant(conn, participant_id=DEMO_USER_ID, amount_micro=2 * POINT_SCALE, idempotency_key="fund-order")
    market_id = sample_markets[0]["market_id"]
    create_collateral_market(conn, market_id=market_id)
    reserved = reserve_v2_order_collateral(conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=1000, idempotency_key="reserve-order")
    released = reserve_v2_order_collateral(conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="NO", quantity=1, limit_price_micro=1000, idempotency_key="release-order")
    cancel_v2_order_collateral(conn, participant_id=DEMO_USER_ID, reservation_id=released["reservation_id"], idempotency_key="cancel-order")
    expected_reservations = [tuple(row) for row in conn.execute("select id, status, release_reason, created_at, released_at from order_collateral_reservations order by id")]
    expected_events = [tuple(row) for row in conn.execute("select reservation_id, event_type, asset_amount from order_collateral_events order by id")]
    expected_balance = tuple(conn.execute("select available_micro, locked_micro from point_accounts where account_id = ?", (allocation["destination_account_id"],)).fetchone())
    conn.close()
    create_backup(source, backup)
    restore_backup(backup, restored, production_db=source)
    restored_conn = connect(str(restored))
    assert [tuple(row) for row in restored_conn.execute("select id, status, release_reason, created_at, released_at from order_collateral_reservations order by id")] == expected_reservations
    assert [tuple(row) for row in restored_conn.execute("select reservation_id, event_type, asset_amount from order_collateral_events order by id")] == expected_events
    assert tuple(restored_conn.execute("select available_micro, locked_micro from point_accounts where account_id = ?", (allocation["destination_account_id"],)).fetchone()) == expected_balance
    assert verify_audit_chain(restored_conn)["integrity_status"] == "verified"
    assert verify_collateral_invariants(restored_conn)["integrity_status"] == "verified"
    assert restored_conn.execute("pragma foreign_key_check").fetchone() is None
    assert reserved["status"] == "reserved"
    restored_conn.close()
