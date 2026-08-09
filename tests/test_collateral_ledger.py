from concurrent.futures import ThreadPoolExecutor
import sqlite3

import pytest

from app.collateral_ledger import (
    ENGINE_KEY,
    POINT_SCALE,
    SQLITE_INTEGER_MAX,
    CollateralLedgerError,
    allocate_v2_points_to_participant,
    cancel_v2_order_collateral,
    bootstrap_v2_point_supply,
    create_collateral_market,
    merge_complete_sets,
    reject_v2_order_collateral,
    reserve_v2_order_collateral,
    split_complete_sets,
    verify_collateral_invariants,
)
from app.storage import DEMO_USER_ID, connect, get_balance, init_db, store_markets, verify_audit_chain


def _market_id(sample_markets):
    market = next(market for market in sample_markets if market["outcomes"] == ["YES", "NO"])
    return market["market_id"]


def _bootstrap_and_market(conn, sample_markets, amount=10_000 * POINT_SCALE):
    treasury = bootstrap_v2_point_supply(conn, amount_micro=amount, idempotency_key="bootstrap-1")
    create_collateral_market(conn, market_id=_market_id(sample_markets))
    return treasury["destination_account_id"], _market_id(sample_markets)


def _position(conn, account_id, market_id, outcome):
    row = conn.execute(
        "select available_shares, locked_shares from outcome_positions where account_id = ? and market_id = ? and outcome = ?",
        (account_id, market_id, outcome),
    ).fetchone()
    return tuple(row) if row else (0, 0)


def _funded_order_account(conn, sample_markets, amount=4 * POINT_SCALE):
    bootstrap_v2_point_supply(conn, amount_micro=amount, idempotency_key="bootstrap-order")
    allocation = allocate_v2_points_to_participant(conn, participant_id=DEMO_USER_ID, amount_micro=amount, idempotency_key="fund-order")
    market_id = _market_id(sample_markets)
    create_collateral_market(conn, market_id=market_id)
    return allocation["destination_account_id"], market_id


def test_bootstrap_credits_only_operator_treasury_and_conserves_points(db_conn):
    legacy_before = get_balance(db_conn, DEMO_USER_ID)
    result = bootstrap_v2_point_supply(db_conn, amount_micro=10_000 * POINT_SCALE, idempotency_key="bootstrap-1")
    account = db_conn.execute("select * from point_accounts where account_id = ?", (result["destination_account_id"],)).fetchone()
    assert account["owner_type"] == "operator"
    assert account["owner_id"] == "operator-treasury"
    assert account["available_micro"] == 10_000 * POINT_SCALE
    assert db_conn.execute("select count(*) from point_accounts where owner_type = 'participant'").fetchone()[0] == 0
    assert get_balance(db_conn, DEMO_USER_ID) == legacy_before
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"
    assert db_conn.execute("select event_type from demo_audit_events order by id desc limit 1").fetchone()[0] == "v2_point_supply_bootstrapped"


@pytest.mark.parametrize("value", [0, -1, 1.0, True, "100"])
def test_bootstrap_rejects_non_positive_or_non_integer_amounts(db_conn, value):
    with pytest.raises(CollateralLedgerError, match="invalid_amount"):
        bootstrap_v2_point_supply(db_conn, amount_micro=value, idempotency_key="bootstrap")


def test_bootstrap_is_idempotent_and_blocks_additional_issue(db_conn):
    first = bootstrap_v2_point_supply(db_conn, amount_micro=99, idempotency_key="bootstrap-1")
    replay = bootstrap_v2_point_supply(db_conn, amount_micro=99, idempotency_key="bootstrap-1")
    assert replay["event_id"] == first["event_id"] and replay["idempotent_replay"]
    with pytest.raises(CollateralLedgerError, match="idempotency_conflict"):
        bootstrap_v2_point_supply(db_conn, amount_micro=100, idempotency_key="bootstrap-1")
    with pytest.raises(CollateralLedgerError, match="bootstrap_already_completed"):
        bootstrap_v2_point_supply(db_conn, amount_micro=100, idempotency_key="bootstrap-2")
    assert db_conn.execute("select count(*) from point_supply_events").fetchone()[0] == 1


def test_bootstrap_rolls_back_when_precommit_invariant_check_fails(db_conn, monkeypatch):
    monkeypatch.setattr("app.collateral_ledger.verify_collateral_invariants", lambda *args, **kwargs: {"integrity_status": "failed"})
    with pytest.raises(CollateralLedgerError, match="invariant_violation"):
        bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    assert db_conn.execute("select count(*) from point_supply_events").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from point_accounts where engine_key = ?", (ENGINE_KEY,)).fetchone()[0] == 0
    assert tuple(db_conn.execute("select issued_micro, bootstrap_completed from point_supply_state where engine_key = ?", (ENGINE_KEY,)).fetchone()) == (0, 0)


def test_collateral_market_requires_existing_binary_yes_no_market(db_conn, sample_markets):
    valid = _market_id(sample_markets)
    assert create_collateral_market(db_conn, market_id=valid)["status"] == "open"
    assert tuple(db_conn.execute("select reserve_micro, net_complete_sets from market_reserves where market_id = ?", (valid,)).fetchone()) == (0, 0)
    assert db_conn.execute("select count(*) from point_supply_events").fetchone()[0] == 0
    assert create_collateral_market(db_conn, market_id=valid)["idempotent_replay"]
    with pytest.raises(CollateralLedgerError, match="market_missing"):
        create_collateral_market(db_conn, market_id="missing")
    non_binary = dict(sample_markets[0], market_id="non-binary", outcomes=["YES", "NO", "MAYBE"])
    store_markets(db_conn, [non_binary])
    with pytest.raises(CollateralLedgerError, match="market_not_open"):
        create_collateral_market(db_conn, market_id="non-binary")


def test_split_and_merge_complete_sets_preserve_all_invariants(db_conn, sample_markets):
    legacy_balance = get_balance(db_conn, DEMO_USER_ID)
    legacy_ledger_count = db_conn.execute("select count(*) from demo_point_ledger").fetchone()[0]
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets)
    split = split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=250, idempotency_key="split-1")
    assert split["points_micro"] == 250 * POINT_SCALE
    reserve = db_conn.execute("select reserve_micro, net_complete_sets from market_reserves where market_id = ?", (market_id,)).fetchone()
    assert tuple(reserve) == (250 * POINT_SCALE, 250)
    assert _position(db_conn, account_id, market_id, "YES") == (250, 0)
    assert _position(db_conn, account_id, market_id, "NO") == (250, 0)
    merge = merge_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=100, idempotency_key="merge-1")
    assert merge["points_micro"] == 100 * POINT_SCALE
    assert tuple(db_conn.execute("select reserve_micro, net_complete_sets from market_reserves where market_id = ?", (market_id,)).fetchone()) == (150 * POINT_SCALE, 150)
    assert _position(db_conn, account_id, market_id, "YES") == (150, 0)
    assert _position(db_conn, account_id, market_id, "NO") == (150, 0)
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (account_id,)).fetchone()[0] == 9_850 * POINT_SCALE
    assert get_balance(db_conn, DEMO_USER_ID) == legacy_balance
    assert db_conn.execute("select count(*) from demo_point_ledger").fetchone()[0] == legacy_ledger_count
    assert verify_collateral_invariants(db_conn, market_id=market_id)["integrity_status"] == "verified"
    assert db_conn.execute("select count(*) from collateral_ledger_entries").fetchone()[0] == 5
    assert verify_audit_chain(db_conn)["integrity_status"] == "verified"


def test_split_idempotency_and_conflict_do_not_duplicate_effect(db_conn, sample_markets):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets)
    first = split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="same")
    replay = split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="same")
    assert replay["event_id"] == first["event_id"] and replay["idempotent_replay"]
    with pytest.raises(CollateralLedgerError, match="idempotency_conflict"):
        merge_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="same")
    assert _position(db_conn, account_id, market_id, "YES") == (1, 0)
    assert db_conn.execute("select count(*) from reserve_events").fetchone()[0] == 1


def test_split_and_merge_reject_insufficient_and_invalid_values(db_conn, sample_markets):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    with pytest.raises(CollateralLedgerError, match="invalid_quantity"):
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=True, idempotency_key="bad")
    with pytest.raises(CollateralLedgerError, match="insufficient_points"):
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=2, idempotency_key="too-many")
    split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")
    db_conn.execute("update outcome_positions set available_shares = 0 where account_id = ? and market_id = ? and outcome = 'YES'", (account_id, market_id))
    with pytest.raises(CollateralLedgerError, match="insufficient_yes_shares"):
        merge_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="merge")


def test_locked_shares_cannot_be_merged(db_conn, sample_markets):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")
    db_conn.execute("update outcome_positions set available_shares = 0, locked_shares = 1 where account_id = ? and market_id = ? and outcome = 'YES'", (account_id, market_id))
    with pytest.raises(CollateralLedgerError, match="locked_shares_present"):
        merge_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="merge")


def test_frozen_market_rejects_complete_set_operations(db_conn, sample_markets):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    db_conn.execute("update collateral_markets set status = 'frozen' where market_id = ?", (market_id,))
    with pytest.raises(CollateralLedgerError, match="market_not_open"):
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")


def test_failed_operation_rolls_back_all_writes(db_conn, sample_markets, monkeypatch):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    monkeypatch.setattr("app.collateral_ledger.verify_collateral_invariants", lambda *args, **kwargs: {"integrity_status": "failed"})
    with pytest.raises(CollateralLedgerError, match="invariant_violation"):
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")
    assert db_conn.execute("select reserve_micro from market_reserves where market_id = ?", (market_id,)).fetchone()[0] == 0
    assert db_conn.execute("select count(*) from reserve_events").fetchone()[0] == 0
    assert _position(db_conn, account_id, market_id, "YES") == (0, 0)


def test_invariant_verifier_detects_tampering_without_identifiers(db_conn, sample_markets):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")
    db_conn.execute("update point_accounts set available_micro = available_micro + 1 where account_id = ?", (account_id,))
    result = verify_collateral_invariants(db_conn, market_id=market_id)
    assert result["integrity_status"] == "failed"
    assert result["violation_codes"] == ["global_point_conservation_failed"]
    assert account_id not in str(result)


def test_invariant_verifier_detects_missing_reserve_for_scoped_market(db_conn, sample_markets):
    _, market_id = _bootstrap_and_market(db_conn, sample_markets)
    db_conn.execute("delete from market_reserves where market_id = ?", (market_id,))

    result = verify_collateral_invariants(db_conn, market_id=market_id)

    assert result["integrity_status"] == "failed"
    assert result["market_count"] == 1
    assert "market_reserve_missing" in result["violation_codes"]


def test_invariant_verifier_detects_missing_reserve_globally(db_conn, sample_markets):
    _, market_id = _bootstrap_and_market(db_conn, sample_markets)
    db_conn.execute("delete from market_reserves where market_id = ?", (market_id,))

    result = verify_collateral_invariants(db_conn)

    assert result["integrity_status"] == "failed"
    assert result["market_count"] >= 1
    assert "market_reserve_missing" in result["violation_codes"]


def test_existing_collateral_market_replay_rejects_missing_reserve_without_repair(db_conn, sample_markets):
    _, market_id = _bootstrap_and_market(db_conn, sample_markets)
    audit_count = db_conn.execute("select count(*) from demo_audit_events").fetchone()[0]
    db_conn.execute("delete from market_reserves where market_id = ?", (market_id,))

    with pytest.raises(CollateralLedgerError, match="invariant_violation"):
        create_collateral_market(db_conn, market_id=market_id)

    assert db_conn.execute("select count(*) from market_reserves where market_id = ?", (market_id,)).fetchone()[0] == 0
    assert db_conn.execute("select count(*) from demo_audit_events").fetchone()[0] == audit_count


def test_existing_collateral_market_replay_verifies_healthy_market(db_conn, sample_markets):
    _, market_id = _bootstrap_and_market(db_conn, sample_markets)

    replay = create_collateral_market(db_conn, market_id=market_id)

    assert replay["idempotent_replay"] is True
    assert verify_collateral_invariants(db_conn, market_id=market_id)["integrity_status"] == "verified"


def test_participant_allocation_transfers_treasury_points_without_issuance(db_conn, sample_markets):
    treasury = bootstrap_v2_point_supply(db_conn, amount_micro=10 * POINT_SCALE, idempotency_key="bootstrap")
    legacy_balance = get_balance(db_conn, DEMO_USER_ID)
    legacy_ledger_count = db_conn.execute("select count(*) from demo_point_ledger").fetchone()[0]

    result = allocate_v2_points_to_participant(
        db_conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="allocation"
    )

    assert result == {
        "event_id": 1,
        "participant_id": DEMO_USER_ID,
        "destination_account_id": f"{ENGINE_KEY}:participant:{DEMO_USER_ID}",
        "amount_micro": POINT_SCALE,
        "participant_available_micro": POINT_SCALE,
        "idempotent_replay": False,
    }
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (treasury["destination_account_id"],)).fetchone()[0] == 9 * POINT_SCALE
    assert tuple(db_conn.execute("select issued_micro, burned_micro from point_supply_state where engine_key = ?", (ENGINE_KEY,)).fetchone()) == (10 * POINT_SCALE, 0)
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 1
    assert db_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 2
    assert db_conn.execute("select count(*) from demo_audit_events where event_type = 'v2_participant_points_allocated'").fetchone()[0] == 1
    assert get_balance(db_conn, DEMO_USER_ID) == legacy_balance
    assert db_conn.execute("select count(*) from demo_point_ledger").fetchone()[0] == legacy_ledger_count
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"
    assert verify_audit_chain(db_conn)["integrity_status"] == "verified"


def test_participant_allocation_replays_and_rejects_invalid_or_corrupt_state(db_conn):
    bootstrap_v2_point_supply(db_conn, amount_micro=2 * POINT_SCALE, idempotency_key="bootstrap")
    first = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="same")
    replay = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="same")
    assert replay == {**first, "idempotent_replay": True}
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 1
    with pytest.raises(CollateralLedgerError, match="idempotency_conflict"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=2 * POINT_SCALE, idempotency_key="same")
    db_conn.execute("update point_accounts set available_micro = available_micro + 1 where account_id = ?", (first["destination_account_id"],))
    with pytest.raises(CollateralLedgerError, match="invariant_violation"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="same")


@pytest.mark.parametrize("participant_id", ["", " ", "***", " operator-treasury ", "operator-treasury", None])
def test_participant_allocation_rejects_invalid_participant_ids(db_conn, participant_id):
    bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    with pytest.raises(CollateralLedgerError, match="invalid_participant_id"):
        allocate_v2_points_to_participant(db_conn, participant_id=participant_id, amount_micro=1, idempotency_key="allocation")


@pytest.mark.parametrize("amount", [0, -1, 1.0, True, "1"])
def test_participant_allocation_validates_amount_and_bootstrap(db_conn, amount):
    with pytest.raises(CollateralLedgerError, match="invalid_amount"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=amount, idempotency_key="allocation")
    if amount == 0:
        with pytest.raises(CollateralLedgerError, match="bootstrap_required"):
            allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="allocation-ready")


def test_participant_allocation_rejects_unknown_insufficient_and_owner_conflict(db_conn):
    bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    with pytest.raises(CollateralLedgerError, match="participant_missing"):
        allocate_v2_points_to_participant(db_conn, participant_id="unknown-user", amount_micro=1, idempotency_key="unknown")
    with pytest.raises(CollateralLedgerError, match="insufficient_points"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE + 1, idempotency_key="short")
    account_id = f"{ENGINE_KEY}:participant:{DEMO_USER_ID}"
    db_conn.execute(
        "insert into point_accounts(account_id, engine_key, owner_type, owner_id, created_at, updated_at) values (?, ?, 'system', 'other', 'x', 'x')",
        (account_id, ENGINE_KEY),
    )
    with pytest.raises(CollateralLedgerError, match="account_owner_conflict"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="conflict")


def test_participant_allocation_rejects_disabled_only_account_and_can_fund_complete_sets(db_conn, sample_markets):
    bootstrap_v2_point_supply(db_conn, amount_micro=2 * POINT_SCALE, idempotency_key="bootstrap")
    db_conn.execute(
        """insert into user_accounts(
            id, email_normalized, email_display, password_hash, participant_id, account_status,
            created_at, updated_at, password_changed_at
        ) values ('disabled', 'disabled@example.test', 'disabled@example.test', 'hash', 'disabled-user', 'disabled', 'x', 'x', 'x')"""
    )
    with pytest.raises(CollateralLedgerError, match="participant_missing"):
        allocate_v2_points_to_participant(db_conn, participant_id="disabled-user", amount_micro=1, idempotency_key="disabled")
    allocation = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="fund")
    market_id = _market_id(sample_markets)
    create_collateral_market(db_conn, market_id=market_id)
    split_complete_sets(db_conn, account_id=allocation["destination_account_id"], market_id=market_id, quantity=1, idempotency_key="participant-split")
    assert verify_collateral_invariants(db_conn, market_id=market_id)["integrity_status"] == "verified"


def test_participant_allocation_rolls_back_when_ledger_write_fails(db_conn, monkeypatch):
    bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    monkeypatch.setattr("app.collateral_ledger._record_ledger", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("ledger failure")))
    with pytest.raises(RuntimeError, match="ledger failure"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="allocation")
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from point_accounts where owner_type = 'participant' and engine_key = ?", (ENGINE_KEY,)).fetchone()[0] == 0


def test_concurrent_participant_allocations_cannot_overspend_treasury(tmp_path):
    path = tmp_path / "allocation.db"
    setup = connect(str(path))
    init_db(setup)
    bootstrap_v2_point_supply(setup, amount_micro=1, idempotency_key="bootstrap")
    setup.execute("insert into demo_users(user_id, balance) values ('participant-2', 0)")
    setup.commit()
    setup.close()

    def allocate(participant):
        conn = connect(str(path))
        try:
            return allocate_v2_points_to_participant(conn, participant_id=participant, amount_micro=1, idempotency_key=participant)["event_id"]
        except CollateralLedgerError as exc:
            return exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(allocate, (DEMO_USER_ID, "participant-2")))
    verify_conn = connect(str(path))
    assert sum(isinstance(result, int) for result in results) == 1
    assert results.count("insufficient_points") == 1
    assert verify_conn.execute("select available_micro from point_accounts where account_id = ?", (f"{ENGINE_KEY}:operator:operator-treasury",)).fetchone()[0] == 0
    assert verify_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 1
    assert verify_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 2
    assert verify_conn.execute("select coalesce(sum(available_micro), 0) from point_accounts where owner_type = 'participant' and engine_key = ?", (ENGINE_KEY,)).fetchone()[0] == 1
    assert verify_conn.execute("select sum(amount_micro) from point_allocation_events").fetchone()[0] == 1
    debit, credit = tuple(verify_conn.execute(
        "select coalesce(sum(case when entry_type = 'participant_allocation_debit' then amount_micro end), 0), "
        "coalesce(sum(case when entry_type = 'participant_allocation_credit' then amount_micro end), 0) "
        "from collateral_ledger_entries where reference_type = 'point_allocation_event'"
    ).fetchone())
    assert (debit, credit) == (-1, 1)
    assert abs(debit) == credit
    assert verify_collateral_invariants(verify_conn)["integrity_status"] == "verified"
    verify_conn.close()


def test_participant_allocation_rolls_back_when_event_insert_fails(db_conn):
    treasury = bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    db_conn.execute("create trigger fail_allocation_event before insert on point_allocation_events begin select raise(abort, 'event failure'); end")
    with pytest.raises(sqlite3.IntegrityError, match="event failure"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="allocation")
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (treasury["destination_account_id"],)).fetchone()[0] == POINT_SCALE
    assert db_conn.execute("select count(*) from point_accounts where owner_type = 'participant' and engine_key = ?", (ENGINE_KEY,)).fetchone()[0] == 0
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from demo_audit_events where event_type = 'v2_participant_points_allocated'").fetchone()[0] == 0
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"


def test_participant_allocation_rolls_back_when_audit_or_postwrite_invariant_fails(db_conn, monkeypatch):
    treasury = bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    monkeypatch.setattr("app.collateral_ledger.insert_audit_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit failure")))
    with pytest.raises(RuntimeError, match="audit failure"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="audit")
    monkeypatch.undo()
    calls = iter(({"integrity_status": "verified"}, {"integrity_status": "failed"}))
    monkeypatch.setattr("app.collateral_ledger.verify_collateral_invariants", lambda *args, **kwargs: next(calls))
    with pytest.raises(CollateralLedgerError, match="invariant_violation"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="postwrite")
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (treasury["destination_account_id"],)).fetchone()[0] == POINT_SCALE
    assert db_conn.execute("select count(*) from point_accounts where owner_type = 'participant' and engine_key = ?", (ENGINE_KEY,)).fetchone()[0] == 0
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from demo_audit_events where event_type = 'v2_participant_points_allocated'").fetchone()[0] == 0


def test_participant_allocation_rejects_destination_overflow_without_writes(db_conn, monkeypatch):
    treasury = bootstrap_v2_point_supply(db_conn, amount_micro=1, idempotency_key="bootstrap")
    destination_id = f"{ENGINE_KEY}:participant:{DEMO_USER_ID}"
    db_conn.execute(
        "insert into point_accounts(account_id, engine_key, owner_type, owner_id, available_micro, created_at, updated_at) values (?, ?, 'participant', ?, ?, 'x', 'x')",
        (destination_id, ENGINE_KEY, DEMO_USER_ID, SQLITE_INTEGER_MAX),
    )
    monkeypatch.setattr("app.collateral_ledger.verify_collateral_invariants", lambda *args, **kwargs: {"integrity_status": "verified"})
    with pytest.raises(CollateralLedgerError, match="integer_overflow"):
        allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="overflow")
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (destination_id,)).fetchone()[0] == SQLITE_INTEGER_MAX
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (treasury["destination_account_id"],)).fetchone()[0] == 1
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from demo_audit_events where event_type = 'v2_participant_points_allocated'").fetchone()[0] == 0


def test_participant_allocation_idempotency_key_cannot_target_another_participant(db_conn):
    bootstrap_v2_point_supply(db_conn, amount_micro=2, idempotency_key="bootstrap")
    db_conn.execute("insert into demo_users(user_id, balance) values ('participant-2', 0)")
    first = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=1, idempotency_key="same-key")
    with pytest.raises(CollateralLedgerError, match="idempotency_conflict"):
        allocate_v2_points_to_participant(db_conn, participant_id="participant-2", amount_micro=1, idempotency_key="same-key")
    assert db_conn.execute("select count(*) from point_accounts where account_id = ?", (f"{ENGINE_KEY}:participant:participant-2",)).fetchone()[0] == 0
    assert db_conn.execute("select count(*) from point_allocation_events").fetchone()[0] == 1
    assert db_conn.execute("select count(*) from collateral_ledger_entries where reference_type = 'point_allocation_event'").fetchone()[0] == 2
    assert db_conn.execute("select count(*) from demo_audit_events where event_type = 'v2_participant_points_allocated'").fetchone()[0] == 1
    assert first["participant_available_micro"] == 1


def test_participant_allocation_account_can_split_and_merge_complete_sets(db_conn, sample_markets):
    bootstrap_v2_point_supply(db_conn, amount_micro=3 * POINT_SCALE, idempotency_key="bootstrap")
    allocation = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=2 * POINT_SCALE, idempotency_key="allocation")
    market_id = _market_id(sample_markets)
    create_collateral_market(db_conn, market_id=market_id)
    split_complete_sets(db_conn, account_id=allocation["destination_account_id"], market_id=market_id, quantity=2, idempotency_key="split")
    merge_complete_sets(db_conn, account_id=allocation["destination_account_id"], market_id=market_id, quantity=1, idempotency_key="merge")
    assert db_conn.execute("select available_micro from point_accounts where account_id = ?", (allocation["destination_account_id"],)).fetchone()[0] == POINT_SCALE
    assert _position(db_conn, allocation["destination_account_id"], market_id, "YES") == (1, 0)
    assert _position(db_conn, allocation["destination_account_id"], market_id, "NO") == (1, 0)
    assert tuple(db_conn.execute("select reserve_micro, net_complete_sets from market_reserves where market_id = ?", (market_id,)).fetchone()) == (POINT_SCALE, 1)
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"
    assert verify_audit_chain(db_conn)["integrity_status"] == "verified"


def test_order_collateral_buy_reserve_cancel_and_reject_preserve_invariants(db_conn, sample_markets):
    bootstrap_v2_point_supply(db_conn, amount_micro=4 * POINT_SCALE, idempotency_key="bootstrap")
    allocation = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=2 * POINT_SCALE, idempotency_key="fund")
    market_id = _market_id(sample_markets)
    create_collateral_market(db_conn, market_id=market_id)
    reserved = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=2, limit_price_micro=1000, idempotency_key="reserve")
    assert reserved["collateral_amount"] == 2000 and reserved["status"] == "reserved"
    cancelled = cancel_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, reservation_id=reserved["reservation_id"], idempotency_key="cancel")
    assert cancelled["status"] == "released" and cancelled["release_reason"] == "cancelled"
    assert cancel_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, reservation_id=reserved["reservation_id"], idempotency_key="cancel")["idempotent_replay"]
    second = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="NO", quantity=1, limit_price_micro=1000, idempotency_key="reserve-2")
    assert reject_v2_order_collateral(db_conn, reservation_id=second["reservation_id"], idempotency_key="reject")["release_reason"] == "rejected"
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"
    assert verify_audit_chain(db_conn)["integrity_status"] == "verified"


def test_order_collateral_sell_reserve_and_cancel(db_conn, sample_markets):
    bootstrap_v2_point_supply(db_conn, amount_micro=3 * POINT_SCALE, idempotency_key="bootstrap")
    allocation = allocate_v2_points_to_participant(db_conn, participant_id=DEMO_USER_ID, amount_micro=2 * POINT_SCALE, idempotency_key="fund")
    market_id = _market_id(sample_markets)
    create_collateral_market(db_conn, market_id=market_id)
    split_complete_sets(db_conn, account_id=allocation["destination_account_id"], market_id=market_id, quantity=2, idempotency_key="split")
    reserved = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="SELL", outcome="YES", quantity=2, limit_price_micro=POINT_SCALE, idempotency_key="sell")
    assert _position(db_conn, allocation["destination_account_id"], market_id, "YES") == (0, 2)
    cancel_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, reservation_id=reserved["reservation_id"], idempotency_key="cancel")
    assert _position(db_conn, allocation["destination_account_id"], market_id, "YES") == (2, 0)
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"


@pytest.mark.parametrize("side,outcome,quantity,price,code", [
    ("buy", "YES", 1, 1, "invalid_side"), ("BUY", "yes", 1, 1, "invalid_outcome"),
    ("BUY", "YES", 0, 1, "invalid_quantity"), ("BUY", "YES", 1, 0, "invalid_limit_price"),
    ("BUY", "YES", True, 1, "invalid_quantity"), ("BUY", "YES", 1, "1", "invalid_limit_price"),
])
def test_order_collateral_rejects_strict_inputs(db_conn, sample_markets, side, outcome, quantity, price, code):
    _funded_order_account(db_conn, sample_markets)
    with pytest.raises(CollateralLedgerError, match=code):
        reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=_market_id(sample_markets), side=side, outcome=outcome, quantity=quantity, limit_price_micro=price, idempotency_key="key")


def test_order_collateral_reserve_replay_conflicts_and_tamper_detection(db_conn, sample_markets):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    first = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="same")
    assert reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="same")["idempotent_replay"]
    with pytest.raises(CollateralLedgerError, match="idempotency_conflict"):
        reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="NO", quantity=1, limit_price_micro=100, idempotency_key="same")
    db_conn.execute("update point_accounts set locked_micro = 0 where account_id = ?", (account_id,))
    result = verify_collateral_invariants(db_conn)
    assert "buy_locked_points_mismatch" in result["violation_codes"]
    assert account_id not in str(result) and DEMO_USER_ID not in str(result)
    assert first["reservation_id"] == 1


def test_order_collateral_release_rolls_back_on_event_failure(db_conn, sample_markets):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="reserve")
    db_conn.execute("create trigger fail_release_event before insert on order_collateral_events when new.event_type = 'release' begin select raise(abort, 'release event failure'); end")
    with pytest.raises(sqlite3.IntegrityError, match="release event failure"):
        cancel_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, reservation_id=reservation["reservation_id"], idempotency_key="cancel")
    row = db_conn.execute("select status, release_reason, released_at from order_collateral_reservations where id = ?", (reservation["reservation_id"],)).fetchone()
    assert tuple(row) == ("reserved", None, None)
    assert db_conn.execute("select locked_micro from point_accounts where account_id = ?", (account_id,)).fetchone()[0] == 100
    assert db_conn.execute("select count(*) from order_collateral_events where event_type = 'release'").fetchone()[0] == 0


@pytest.mark.parametrize(
    ("tamper_sql", "expected_code"),
    [
        ("update market_reserves set reserve_micro = 0 where market_id = ?", "reserve_complete_set_mismatch"),
        ("update outcome_positions set available_shares = 0 where account_id = ? and market_id = ? and outcome = 'YES'", "yes_supply_mismatch"),
        ("update outcome_positions set available_shares = 0 where account_id = ? and market_id = ? and outcome = 'NO'", "no_supply_mismatch"),
    ],
)
def test_invariant_verifier_detects_market_supply_tampering(db_conn, sample_markets, tamper_sql, expected_code):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")
    if "market_reserves" in tamper_sql:
        db_conn.execute("pragma ignore_check_constraints = on")
        db_conn.execute(tamper_sql, (market_id,))
        db_conn.execute("pragma ignore_check_constraints = off")
    else:
        db_conn.execute(tamper_sql, (account_id, market_id))
    assert expected_code in verify_collateral_invariants(db_conn, market_id=market_id)["violation_codes"]


def test_integer_overflow_is_rejected_before_write(db_conn, sample_markets):
    account_id, market_id = _bootstrap_and_market(db_conn, sample_markets, amount=POINT_SCALE)
    with pytest.raises(CollateralLedgerError, match="integer_overflow"):
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id,
                            quantity=SQLITE_INTEGER_MAX // POINT_SCALE + 1, idempotency_key="overflow")
    assert db_conn.execute("select count(*) from reserve_events").fetchone()[0] == 0


def test_concurrent_split_cannot_overspend(tmp_path, sample_markets):
    path = tmp_path / "collateral.db"
    setup = connect(str(path))
    init_db(setup)
    store_markets(setup, sample_markets)
    treasury = bootstrap_v2_point_supply(setup, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    market_id = _market_id(sample_markets)
    create_collateral_market(setup, market_id=market_id)
    account_id = treasury["destination_account_id"]
    setup.close()

    def split_once(key):
        conn = connect(str(path))
        try:
            split_complete_sets(conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key=key)
            return "success"
        except CollateralLedgerError as exc:
            return exc.code
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(split_once, ("split-a", "split-b")))
    assert sorted(results) == ["insufficient_points", "success"]
    verify_conn = connect(str(path))
    assert verify_collateral_invariants(verify_conn, market_id=market_id)["integrity_status"] == "verified"
    assert verify_conn.execute("select reserve_micro from market_reserves where market_id = ?", (market_id,)).fetchone()[0] == POINT_SCALE
    verify_conn.close()


def test_concurrent_order_collateral_buy_overspend_and_cancel_reject_release_once(tmp_path, sample_markets):
    path = tmp_path / "order-collateral-buy.db"
    setup = connect(str(path)); init_db(setup); store_markets(setup, sample_markets)
    bootstrap_v2_point_supply(setup, amount_micro=POINT_SCALE, idempotency_key="bootstrap")
    allocation = allocate_v2_points_to_participant(setup, participant_id=DEMO_USER_ID, amount_micro=POINT_SCALE, idempotency_key="fund")
    market_id = _market_id(sample_markets); create_collateral_market(setup, market_id=market_id); setup.close()

    def reserve(key):
        conn = connect(str(path))
        try:
            return reserve_v2_order_collateral(conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=POINT_SCALE, idempotency_key=key)["reservation_id"]
        except CollateralLedgerError as exc:
            return exc.code
        finally: conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("buy-a", "buy-b")))
    assert sum(isinstance(value, int) for value in results) == 1
    assert next(value for value in results if not isinstance(value, int)) in {"insufficient_points", "concurrent_update"}
    reservation_id = next(value for value in results if isinstance(value, int))

    def release(kind):
        conn = connect(str(path))
        try:
            return (cancel_v2_order_collateral if kind == "cancel" else reject_v2_order_collateral)(conn, **({"participant_id": DEMO_USER_ID} if kind == "cancel" else {}), reservation_id=reservation_id, idempotency_key=kind)["release_reason"]
        except CollateralLedgerError as exc:
            return exc.code
        finally: conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        releases = list(pool.map(release, ("cancel", "reject")))
    assert sum(value in {"cancelled", "rejected"} for value in releases) == 1
    assert next(value for value in releases if value not in {"cancelled", "rejected"}) in {"reservation_not_reserved", "concurrent_update"}
    verify = connect(str(path))
    assert tuple(verify.execute("select available_micro, locked_micro from point_accounts where account_id = ?", (allocation["destination_account_id"],)).fetchone()) == (POINT_SCALE, 0)
    assert verify.execute("select count(*) from order_collateral_events where event_type = 'release'").fetchone()[0] == 1
    assert verify.execute("select count(*) from order_collateral_ledger_entries where event_id in (select id from order_collateral_events where event_type = 'release')").fetchone()[0] == 2
    assert verify_collateral_invariants(verify)["integrity_status"] == "verified"
    assert verify_audit_chain(verify)["integrity_status"] == "verified"
    verify.close()


def test_concurrent_order_collateral_sell_oversell_keeps_exactly_one_lock(tmp_path, sample_markets):
    path = tmp_path / "order-collateral-sell.db"
    setup = connect(str(path)); init_db(setup); store_markets(setup, sample_markets)
    bootstrap_v2_point_supply(setup, amount_micro=3 * POINT_SCALE, idempotency_key="bootstrap")
    allocation = allocate_v2_points_to_participant(setup, participant_id=DEMO_USER_ID, amount_micro=2 * POINT_SCALE, idempotency_key="fund")
    market_id = _market_id(sample_markets); create_collateral_market(setup, market_id=market_id)
    split_complete_sets(setup, account_id=allocation["destination_account_id"], market_id=market_id, quantity=2, idempotency_key="split")
    setup.close()
    def reserve(key):
        conn = connect(str(path))
        try:
            return reserve_v2_order_collateral(conn, participant_id=DEMO_USER_ID, market_id=market_id, side="SELL", outcome="YES", quantity=2, limit_price_micro=POINT_SCALE, idempotency_key=key)["reservation_id"]
        except CollateralLedgerError as exc:
            return exc.code
        finally: conn.close()
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(reserve, ("sell-a", "sell-b")))
    assert sum(isinstance(value, int) for value in results) == 1
    assert next(value for value in results if not isinstance(value, int)) in {"insufficient_shares", "concurrent_update"}
    verify = connect(str(path))
    assert _position(verify, allocation["destination_account_id"], market_id, "YES") == (0, 2)
    assert verify.execute("select count(*) from order_collateral_reservations").fetchone()[0] == 1
    assert verify.execute("select count(*) from order_collateral_events where event_type = 'reserve'").fetchone()[0] == 1
    assert verify.execute("select count(*) from order_collateral_ledger_entries").fetchone()[0] == 2
    assert verify.execute("select count(*) from demo_audit_events where event_type = 'v2_order_collateral_reserved'").fetchone()[0] == 1
    assert verify_collateral_invariants(verify)["integrity_status"] == "verified"
    assert verify_audit_chain(verify)["integrity_status"] == "verified"
    verify.close()


def _reserved_order_for_rollback(conn, sample_markets, side):
    account_id, market_id = _funded_order_account(conn, sample_markets)
    if side == "SELL":
        split_complete_sets(conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="rollback-split")
    reservation = reserve_v2_order_collateral(conn, participant_id=DEMO_USER_ID, market_id=market_id, side=side, outcome="YES", quantity=1, limit_price_micro=100, idempotency_key=f"rollback-{side}")
    return account_id, market_id, reservation


@pytest.mark.parametrize("side,operation", [("BUY", "cancel"), ("SELL", "cancel"), ("BUY", "reject"), ("SELL", "reject")])
@pytest.mark.parametrize("stage", ["resource_cas", "reservation_cas", "event_insert", "first_ledger", "second_ledger", "audit", "post_invariant"])
def test_order_collateral_release_write_stages_roll_back_exactly(db_conn, sample_markets, monkeypatch, side, operation, stage):
    account_id, market_id, reservation = _reserved_order_for_rollback(db_conn, sample_markets, side)
    before_reservation = tuple(db_conn.execute("select status, release_reason, released_at, version from order_collateral_reservations where id=?", (reservation["reservation_id"],)).fetchone())
    if side == "BUY":
        before_resource = tuple(db_conn.execute("select available_micro, locked_micro from point_accounts where account_id=?", (account_id,)).fetchone())
    else:
        before_resource = _position(db_conn, account_id, market_id, "YES")
    before_counts = tuple(db_conn.execute("select (select count(*) from order_collateral_events), (select count(*) from order_collateral_ledger_entries), (select count(*) from demo_audit_events)").fetchone())
    if stage == "event_insert":
        db_conn.execute("create trigger fail_release_event_stage before insert on order_collateral_events when new.event_type='release' begin select raise(abort, 'event stage'); end")
        expected = sqlite3.IntegrityError
    elif stage in {"first_ledger", "second_ledger"}:
        target = "available" if stage == "first_ledger" else "locked"
        db_conn.execute(f"create trigger fail_{target}_ledger_stage before insert on order_collateral_ledger_entries when new.balance_bucket='{target}' begin select raise(abort, '{target} stage'); end")
        expected = sqlite3.IntegrityError
    elif stage == "audit":
        monkeypatch.setattr("app.collateral_ledger.insert_audit_event", lambda *a, **k: (_ for _ in ()).throw(RuntimeError("audit stage")))
        expected = RuntimeError
    elif stage == "post_invariant":
        monkeypatch.setattr(
            "app.collateral_ledger.verify_collateral_invariants",
            lambda *a, **k: {"integrity_status": "failed"},
        )
        expected = CollateralLedgerError
    elif stage == "resource_cas":
        table = "point_accounts" if side == "BUY" else "outcome_positions"
        db_conn.execute(f"create trigger fail_{stage}_{side} before update on {table} begin select raise(abort, '{stage}'); end")
        expected = sqlite3.IntegrityError
    else:
        db_conn.execute("create trigger fail_reservation_release_cas before update on order_collateral_reservations begin select raise(abort, 'reservation cas'); end")
        expected = sqlite3.IntegrityError
    release = cancel_v2_order_collateral if operation == "cancel" else reject_v2_order_collateral
    kwargs = {"reservation_id": reservation["reservation_id"], "idempotency_key": f"{operation}-{stage}"}
    if operation == "cancel": kwargs["participant_id"] = DEMO_USER_ID
    with pytest.raises(expected):
        release(db_conn, **kwargs)
    assert tuple(db_conn.execute("select status, release_reason, released_at, version from order_collateral_reservations where id=?", (reservation["reservation_id"],)).fetchone()) == before_reservation
    current = tuple(db_conn.execute("select available_micro, locked_micro from point_accounts where account_id=?", (account_id,)).fetchone()) if side == "BUY" else _position(db_conn, account_id, market_id, "YES")
    assert current == before_resource
    assert tuple(db_conn.execute("select (select count(*) from order_collateral_events), (select count(*) from order_collateral_ledger_entries), (select count(*) from demo_audit_events)").fetchone()) == before_counts
    assert verify_audit_chain(db_conn)["integrity_status"] == "verified"
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"


@pytest.mark.parametrize("side", ["BUY", "SELL"], ids=["buy", "sell"])
@pytest.mark.parametrize("stage", ["resource_cas", "reservation_insert", "event_insert", "available_ledger_insert", "locked_ledger_insert", "audit_insert", "post_invariant"], ids=["resource-cas", "reservation-insert", "event-insert", "available-ledger-insert", "locked-ledger-insert", "audit-insert", "post-invariant"])
def test_order_collateral_reserve_write_stages_roll_back_exactly(db_conn, sample_markets, monkeypatch, side, stage):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    if side == "SELL":
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="reserve-rollback-split")
        before_resource = _position(db_conn, account_id, market_id, "YES")
        resource_table = "outcome_positions"
    else:
        before_resource = tuple(db_conn.execute("select available_micro, locked_micro from point_accounts where account_id=?", (account_id,)).fetchone())
        resource_table = "point_accounts"
    before_counts = tuple(db_conn.execute(
        "select (select count(*) from order_collateral_reservations), (select count(*) from order_collateral_events), "
        "(select count(*) from order_collateral_ledger_entries), (select count(*) from demo_audit_events)"
    ).fetchone())
    if stage == "resource_cas":
        db_conn.execute(f"create trigger fail_reserve_resource_{side} before update on {resource_table} begin select raise(abort, 'resource cas'); end")
        expected = sqlite3.IntegrityError
    elif stage == "reservation_insert":
        db_conn.execute("create trigger fail_reserve_reservation before insert on order_collateral_reservations begin select raise(abort, 'reservation insert'); end")
        expected = sqlite3.IntegrityError
    elif stage == "event_insert":
        db_conn.execute("create trigger fail_reserve_event before insert on order_collateral_events when new.event_type='reserve' begin select raise(abort, 'event insert'); end")
        expected = sqlite3.IntegrityError
    elif stage in {"available_ledger_insert", "locked_ledger_insert"}:
        bucket = "available" if stage == "available_ledger_insert" else "locked"
        db_conn.execute(f"create trigger fail_reserve_{bucket}_ledger before insert on order_collateral_ledger_entries when new.balance_bucket='{bucket}' begin select raise(abort, '{bucket} ledger'); end")
        expected = sqlite3.IntegrityError
    elif stage == "audit_insert":
        monkeypatch.setattr("app.collateral_ledger.insert_audit_event", lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("audit insert")))
        expected = RuntimeError
    else:
        # reserve_v2_order_collateral verifies once before mutation and once after all writes.
        outcomes = iter(({"integrity_status": "verified"}, {"integrity_status": "failed"}))
        monkeypatch.setattr("app.collateral_ledger.verify_collateral_invariants", lambda *args, **kwargs: next(outcomes))
        expected = CollateralLedgerError
    with pytest.raises(expected):
        reserve_v2_order_collateral(
            db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side=side, outcome="YES",
            quantity=1, limit_price_micro=100, idempotency_key=f"reserve-{side}-{stage}",
        )
    current_resource = _position(db_conn, account_id, market_id, "YES") if side == "SELL" else tuple(db_conn.execute("select available_micro, locked_micro from point_accounts where account_id=?", (account_id,)).fetchone())
    assert current_resource == before_resource
    assert tuple(db_conn.execute(
        "select (select count(*) from order_collateral_reservations), (select count(*) from order_collateral_events), "
        "(select count(*) from order_collateral_ledger_entries), (select count(*) from demo_audit_events)"
    ).fetchone()) == before_counts
    assert verify_audit_chain(db_conn)["integrity_status"] == "verified"
    assert verify_collateral_invariants(db_conn)["integrity_status"] == "verified"


@pytest.mark.parametrize("side", ["BUY", "SELL"], ids=["buy", "sell"])
@pytest.mark.parametrize("tamper", ["missing_reserve_event", "unexpected_release_event"], ids=["missing-reserve-event", "unexpected-release-event"])
def test_order_collateral_reservation_event_tamper_is_detected_without_identifier_leak(db_conn, sample_markets, side, tamper):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    if side == "SELL":
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="tamper-split")
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side=side, outcome="YES", quantity=1, limit_price_micro=100, idempotency_key=f"tamper-{side}")
    if tamper == "missing_reserve_event":
        db_conn.execute("delete from order_collateral_events where reservation_id=?", (reservation["reservation_id"],))
    else:
        event = db_conn.execute("select * from order_collateral_events where reservation_id=?", (reservation["reservation_id"],)).fetchone()
        db_conn.execute("insert into order_collateral_events(engine_key,reservation_id,account_id,event_type,release_reason,asset_type,asset_amount,available_before,available_after,locked_before,locked_after,idempotency_key,payload_hash,created_at) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", (ENGINE_KEY,reservation["reservation_id"],account_id,"release","cancelled",event["asset_type"],event["asset_amount"],0,event["asset_amount"],event["asset_amount"],0,"extra", "hash", "x"))
    result = verify_collateral_invariants(db_conn)
    assert result["integrity_status"] == "failed"
    assert "reservation_event_state_mismatch" in result["violation_codes"]
    assert not any(value in str(result).lower() for value in (DEMO_USER_ID, account_id.lower(), "email", "authorization", "cookie", "token", "admin_token"))


@pytest.mark.parametrize("side,operation,bucket", [("BUY", "reserve", "available"), ("BUY", "reserve", "locked"), ("SELL", "reserve", "available"), ("SELL", "reserve", "locked"), ("BUY", "cancel", "available"), ("BUY", "reject", "locked"), ("SELL", "cancel", "available"), ("SELL", "reject", "locked")], ids=["buy-reserve-available", "buy-reserve-locked", "sell-reserve-available", "sell-reserve-locked", "buy-cancel-available", "buy-reject-locked", "sell-cancel-available", "sell-reject-locked"])
def test_order_collateral_ledger_row_tamper_is_detected(db_conn, sample_markets, side, operation, bucket):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    if side == "SELL":
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="ledger-split")
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side=side, outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="ledger-reserve")
    if operation != "reserve":
        (cancel_v2_order_collateral if operation == "cancel" else reject_v2_order_collateral)(db_conn, **({"participant_id": DEMO_USER_ID} if operation == "cancel" else {}), reservation_id=reservation["reservation_id"], idempotency_key=f"ledger-{operation}")
    event_id = db_conn.execute("select id from order_collateral_events where reservation_id=? and event_type=?", (reservation["reservation_id"], "reserve" if operation == "reserve" else "release")).fetchone()[0]
    db_conn.execute("delete from order_collateral_ledger_entries where event_id=? and balance_bucket=?", (event_id, bucket))
    result = verify_collateral_invariants(db_conn)
    assert result["integrity_status"] == "failed"
    assert "order_collateral_ledger_mismatch" in result["violation_codes"]
    assert not any(value in str(result).lower() for value in (DEMO_USER_ID, account_id.lower(), "email", "authorization", "cookie", "token", "admin_token"))


@pytest.mark.parametrize("side,operation,removed_event", [("BUY", "cancel", "reserve"), ("SELL", "cancel", "release"), ("BUY", "reject", "release"), ("SELL", "reject", "reserve")], ids=["buy-cancel-missing-reserve", "sell-cancel-missing-release", "buy-reject-missing-release", "sell-reject-missing-reserve"])
def test_released_order_collateral_event_state_tamper_is_detected(db_conn, sample_markets, side, operation, removed_event):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    if side == "SELL":
        split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="released-tamper-split")
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side=side, outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="released-tamper-reserve")
    (cancel_v2_order_collateral if operation == "cancel" else reject_v2_order_collateral)(db_conn, **({"participant_id": DEMO_USER_ID} if operation == "cancel" else {}), reservation_id=reservation["reservation_id"], idempotency_key="released-tamper-release")
    db_conn.execute("delete from order_collateral_events where reservation_id=? and event_type=?", (reservation["reservation_id"], removed_event))
    result = verify_collateral_invariants(db_conn)
    assert result["integrity_status"] == "failed"
    assert "reservation_event_state_mismatch" in result["violation_codes"]
    assert not any(value in str(result).lower() for value in (DEMO_USER_ID, account_id.lower(), "email", "authorization", "cookie", "token", "admin_token"))


def test_released_order_collateral_extra_release_event_is_detected(db_conn, sample_markets):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="extra-release-reserve")
    cancel_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, reservation_id=reservation["reservation_id"], idempotency_key="extra-release-cancel")
    release = db_conn.execute("select * from order_collateral_events where reservation_id=? and event_type='release'", (reservation["reservation_id"],)).fetchone()
    db_conn.execute(
        """insert into order_collateral_events(
            engine_key,reservation_id,account_id,event_type,release_reason,asset_type,asset_amount,
            available_before,available_after,locked_before,locked_after,idempotency_key,payload_hash,created_at
        ) values (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (ENGINE_KEY, reservation["reservation_id"], account_id, "release", "cancelled", release["asset_type"],
         release["asset_amount"], release["available_before"], release["available_after"], release["locked_before"],
         release["locked_after"], "extra-release-event", "extra-release-hash", "x"),
    )
    result = verify_collateral_invariants(db_conn)
    assert result["integrity_status"] == "failed"
    assert "reservation_event_state_mismatch" in result["violation_codes"]
    assert not any(value in str(result).lower() for value in (DEMO_USER_ID, account_id.lower(), "email", "authorization", "cookie", "token", "admin_token"))


def test_order_collateral_orphan_point_and_share_locks_are_detected(db_conn, sample_markets):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    db_conn.execute("update point_accounts set available_micro = available_micro - 1, locked_micro = locked_micro + 1 where account_id=?", (account_id,))
    point_result = verify_collateral_invariants(db_conn)
    assert point_result["integrity_status"] == "failed"
    assert "buy_locked_points_mismatch" in point_result["violation_codes"]
    db_conn.execute("update point_accounts set available_micro = available_micro + 1, locked_micro = locked_micro - 1 where account_id=?", (account_id,))
    split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="orphan-split")
    db_conn.execute("update outcome_positions set available_shares = available_shares - 1, locked_shares = locked_shares + 1 where account_id=? and market_id=? and outcome='YES'", (account_id, market_id))
    share_result = verify_collateral_invariants(db_conn)
    assert share_result["integrity_status"] == "failed"
    assert "sell_locked_shares_mismatch" in share_result["violation_codes"]
    for result in (point_result, share_result):
        assert not any(value in str(result).lower() for value in (DEMO_USER_ID, account_id.lower(), "email", "authorization", "cookie", "token", "admin_token"))


@pytest.mark.parametrize("sql,params,code", [
    ("update point_accounts set locked_micro = 0 where account_id = ?", lambda r, a: (a,), "buy_locked_points_mismatch"),
    ("update point_accounts set locked_micro = locked_micro + 1 where account_id = ?", lambda r, a: (a,), "buy_locked_points_mismatch"),
])
def test_order_collateral_buy_tamper_variants_are_detected(db_conn, sample_markets, sql, params, code):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="BUY", outcome="YES", quantity=1, limit_price_micro=100, idempotency_key="reserve")
    db_conn.execute(sql, params(reservation, account_id))
    result = verify_collateral_invariants(db_conn)
    assert result["violation_codes"] == [code] or code in result["violation_codes"]
    assert not any(secret in str(result).lower() for secret in (DEMO_USER_ID, account_id.lower(), "email", "token", "cookie", "authorization"))


@pytest.mark.parametrize("sql,params", [
    ("update outcome_positions set locked_shares = 0 where account_id = ? and market_id = ? and outcome = 'YES'", lambda r, a, m: (a, m)),
    ("update outcome_positions set locked_shares = locked_shares + 1 where account_id = ? and market_id = ? and outcome = 'YES'", lambda r, a, m: (a, m)),
])
def test_order_collateral_sell_tamper_variants_are_detected(db_conn, sample_markets, sql, params):
    account_id, market_id = _funded_order_account(db_conn, sample_markets)
    split_complete_sets(db_conn, account_id=account_id, market_id=market_id, quantity=1, idempotency_key="split")
    reservation = reserve_v2_order_collateral(db_conn, participant_id=DEMO_USER_ID, market_id=market_id, side="SELL", outcome="YES", quantity=1, limit_price_micro=POINT_SCALE, idempotency_key="reserve")
    db_conn.execute(sql, params(reservation, account_id, market_id))
    assert "sell_locked_shares_mismatch" in verify_collateral_invariants(db_conn)["violation_codes"]


def _order_ledger_row_for_semantic_tamper(conn, sample_markets, *, side, operation, bucket):
    """Create one healthy order-event ledger row through the production API."""
    account_id, market_id = _funded_order_account(conn, sample_markets)
    if side == "SELL":
        split_complete_sets(conn, account_id=account_id, market_id=market_id, quantity=2, idempotency_key="semantic-tamper-split")
    reservation = reserve_v2_order_collateral(
        conn,
        participant_id=DEMO_USER_ID,
        market_id=market_id,
        side=side,
        outcome="YES",
        quantity=1,
        limit_price_micro=100,
        idempotency_key="semantic-tamper-reserve",
    )
    if operation != "reserve":
        release = cancel_v2_order_collateral if operation == "cancel" else reject_v2_order_collateral
        kwargs = {"reservation_id": reservation["reservation_id"], "idempotency_key": f"semantic-tamper-{operation}"}
        if operation == "cancel":
            kwargs["participant_id"] = DEMO_USER_ID
        release(conn, **kwargs)
    event_type = "reserve" if operation == "reserve" else "release"
    row = conn.execute(
        """select l.* from order_collateral_ledger_entries l
           join order_collateral_events e on e.id = l.event_id
           where e.reservation_id = ? and e.event_type = ? and l.balance_bucket = ?""",
        (reservation["reservation_id"], event_type, bucket),
    ).fetchone()
    assert row is not None
    return account_id, market_id, reservation, row


def _assert_order_ledger_semantic_tamper_detected(conn, account_id):
    result = verify_collateral_invariants(conn)
    assert result["integrity_status"] == "failed"
    assert "order_collateral_ledger_mismatch" in result["violation_codes"]
    serialized = str(result).lower()
    assert not any(value in serialized for value in (
        DEMO_USER_ID,
        account_id.lower(),
        "participant_id",
        "account_id",
        "email",
        "authorization",
        "cookie",
        "token",
        "admin_token",
    ))


@pytest.mark.parametrize(
    "side,operation,bucket",
    [
        ("BUY", "reserve", "available"),
        ("SELL", "reserve", "locked"),
        ("BUY", "cancel", "locked"),
        ("SELL", "reject", "available"),
    ],
    ids=[
        "buy-reserve-delta-available-debit",
        "sell-reserve-delta-locked-credit",
        "buy-cancel-delta-locked-debit",
        "sell-reject-delta-available-credit",
    ],
)
def test_order_collateral_ledger_delta_semantic_tamper_is_detected(
    db_conn, sample_markets, side, operation, bucket
):
    account_id, _, _, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side=side, operation=operation, bucket=bucket
    )
    # The arithmetic CHECK requires balance_after to follow the changed delta.
    db_conn.execute(
        """update order_collateral_ledger_entries
           set delta = ?, balance_after = ? where id = ?""",
        (row["delta"] + 1, row["balance_after"] + 1, row["id"]),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)


@pytest.mark.parametrize(
    "side,operation,bucket",
    [("SELL", "cancel", "available")],
    ids=["sell-cancel-balance-before-available-credit"],
)
def test_order_collateral_ledger_balance_before_semantic_tamper_is_detected(
    db_conn, sample_markets, side, operation, bucket
):
    account_id, _, _, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side=side, operation=operation, bucket=bucket
    )
    # balance_after is the minimum companion change required by the arithmetic CHECK.
    db_conn.execute(
        """update order_collateral_ledger_entries
           set balance_before = ?, balance_after = ? where id = ?""",
        (row["balance_before"] + 1, row["balance_after"] + 1, row["id"]),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)


@pytest.mark.parametrize(
    "side,operation,bucket",
    [("BUY", "reject", "locked")],
    ids=["buy-reject-balance-after-locked-debit"],
)
def test_order_collateral_ledger_balance_after_semantic_tamper_is_detected(
    db_conn, sample_markets, side, operation, bucket
):
    account_id, _, _, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side=side, operation=operation, bucket=bucket
    )
    # balance_before is the minimum companion change required by the arithmetic CHECK.
    db_conn.execute(
        """update order_collateral_ledger_entries
           set balance_before = ?, balance_after = ? where id = ?""",
        (row["balance_before"] + 1, row["balance_after"] + 1, row["id"]),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)


@pytest.mark.parametrize(
    "side,operation,bucket,replacement_asset",
    [("BUY", "reserve", "available", "share"), ("SELL", "cancel", "locked", "point")],
    ids=["buy-reserve-asset-type-point-to-share", "sell-cancel-asset-type-share-to-point"],
)
def test_order_collateral_ledger_asset_type_semantic_tamper_is_detected(
    db_conn, sample_markets, side, operation, bucket, replacement_asset
):
    account_id, _, _, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side=side, operation=operation, bucket=bucket
    )
    db_conn.execute(
        "update order_collateral_ledger_entries set asset_type = ? where id = ?",
        (replacement_asset, row["id"]),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)


def test_order_collateral_ledger_reservation_linkage_semantic_tamper_is_detected(db_conn, sample_markets):
    account_id, market_id, reservation, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side="BUY", operation="reserve", bucket="available"
    )
    alternate = reserve_v2_order_collateral(
        db_conn,
        participant_id=DEMO_USER_ID,
        market_id=market_id,
        side="BUY",
        outcome="NO",
        quantity=1,
        limit_price_micro=100,
        idempotency_key="semantic-tamper-alternate-reservation",
    )
    assert alternate["reservation_id"] != reservation["reservation_id"]
    db_conn.execute(
        "update order_collateral_ledger_entries set reservation_id = ? where id = ?",
        (alternate["reservation_id"], row["id"]),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)


def test_order_collateral_ledger_market_linkage_semantic_tamper_is_detected(db_conn, sample_markets):
    account_id, market_id, _, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side="SELL", operation="reserve", bucket="available"
    )
    source_market = next(market for market in sample_markets if market["market_id"] == market_id)
    alternate_market = {**source_market, "market_id": f"{market_id}-ledger-tamper"}
    store_markets(db_conn, [alternate_market])
    create_collateral_market(db_conn, market_id=alternate_market["market_id"])
    db_conn.execute(
        "update order_collateral_ledger_entries set market_id = ? where id = ?",
        (alternate_market["market_id"], row["id"]),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)


@pytest.mark.parametrize(
    "side,operation,bucket",
    [("BUY", "reject", "available")],
    ids=["buy-reject-outcome-yes-to-no"],
)
def test_order_collateral_ledger_outcome_linkage_semantic_tamper_is_detected(
    db_conn, sample_markets, side, operation, bucket
):
    account_id, _, _, row = _order_ledger_row_for_semantic_tamper(
        db_conn, sample_markets, side=side, operation=operation, bucket=bucket
    )
    db_conn.execute(
        "update order_collateral_ledger_entries set outcome = 'NO' where id = ?",
        (row["id"],),
    )
    _assert_order_ledger_semantic_tamper_detected(db_conn, account_id)
