from concurrent.futures import ThreadPoolExecutor

import pytest

from app.collateral_ledger import (
    ENGINE_KEY,
    POINT_SCALE,
    SQLITE_INTEGER_MAX,
    CollateralLedgerError,
    allocate_v2_points_to_participant,
    bootstrap_v2_point_supply,
    create_collateral_market,
    merge_complete_sets,
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
    assert verify_collateral_invariants(verify_conn)["integrity_status"] == "verified"
    verify_conn.close()


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
