from concurrent.futures import ThreadPoolExecutor
from threading import Barrier

import pytest

import app.demo_points as demo_points
from app.demo_points import DemoPredictionError, create_demo_prediction
from app.storage import (
    INITIAL_DEMO_POINTS,
    DEMO_USER_ID,
    connect,
    ensure_demo_user,
    get_balance,
    init_db,
    list_ledger,
    list_orders,
    list_positions,
    store_markets,
    verify_audit_chain,
)


def test_demo_user_initial_balance(db_conn):
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS


def test_successful_demo_prediction_subtracts_points(db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    result = create_demo_prediction(db_conn, market_id=market_id, outcome="YES", stake=100)
    assert result["balance"] == INITIAL_DEMO_POINTS - 100
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100
    assert list_positions(db_conn)[0]["stake"] == 100
    assert list_ledger(db_conn)[0]["amount"] == -100


def test_insufficient_points_rejected(db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    try:
        create_demo_prediction(db_conn, market_id=market_id, outcome="YES", stake=100000)
    except DemoPredictionError as exc:
        assert "insufficient" in str(exc)
    else:
        raise AssertionError("expected insufficient demo points rejection")


def test_invalid_outcome_rejected(db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    try:
        create_demo_prediction(db_conn, market_id=market_id, outcome="MAYBE", stake=100)
    except DemoPredictionError as exc:
        assert "invalid outcome" in str(exc)
    else:
        raise AssertionError("expected invalid outcome rejection")


def _prediction_snapshot(conn, user_id=DEMO_USER_ID):
    return {
        "balance": get_balance(conn, user_id),
        "orders": len(list_orders(conn, user_id)),
        "positions": len(list_positions(conn, user_id)),
        "prediction_ledger": len([entry for entry in list_ledger(conn, user_id) if entry["entry_type"] == "prediction"]),
        "settlements": conn.execute("select count(*) from demo_settlements where user_id = ?", (user_id,)).fetchone()[0],
        "audit_chain": verify_audit_chain(conn),
    }


def test_prediction_idempotency_replays_same_normalized_payload(db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    first = create_demo_prediction(
        db_conn, market_id=market_id, outcome="YES", stake="10.0", idempotency_key="same-payload"
    )
    replay = create_demo_prediction(
        db_conn, market_id=market_id, outcome="YES", stake=10, idempotency_key="same-payload"
    )

    assert first["idempotent_replay"] is False
    assert replay["idempotent_replay"] is True
    assert replay["position"]["id"] == first["position"]["id"]
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 10
    assert len(list_positions(db_conn)) == 1


@pytest.mark.parametrize(
    ("market_id", "outcome", "stake"),
    [
        ("other-market", "YES", 10),
        (None, "NO", 10),
        (None, "YES", 11),
    ],
    ids=["market", "outcome", "stake"],
)
def test_prediction_idempotency_rejects_payload_conflict_without_side_effects(
    db_conn, sample_markets, market_id, outcome, stake
):
    original_market = sample_markets[0]["market_id"]
    if market_id == "other-market":
        other = dict(sample_markets[1])
        other["market_id"] = market_id
        store_markets(db_conn, [other])
    create_demo_prediction(
        db_conn, market_id=original_market, outcome="YES", stake=10, idempotency_key="payload-conflict"
    )
    before = _prediction_snapshot(db_conn)

    with pytest.raises(DemoPredictionError) as exc_info:
        create_demo_prediction(
            db_conn,
            market_id=market_id or original_market,
            outcome=outcome,
            stake=stake,
            idempotency_key="payload-conflict",
        )

    assert exc_info.value.status_code == 409
    assert _prediction_snapshot(db_conn) == before


@pytest.mark.parametrize("stake", ["NaN", "Infinity", float("nan"), float("inf")])
def test_prediction_rejects_non_finite_stake(db_conn, sample_markets, stake):
    with pytest.raises(DemoPredictionError, match="finite"):
        create_demo_prediction(db_conn, market_id=sample_markets[0]["market_id"], outcome="YES", stake=stake)


def test_prediction_idempotency_key_is_scoped_to_user(db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    ensure_demo_user(db_conn, "other-user")
    db_conn.commit()

    first = create_demo_prediction(
        db_conn, market_id=market_id, outcome="YES", stake=10, user_id=DEMO_USER_ID, idempotency_key="shared-key"
    )
    second = create_demo_prediction(
        db_conn, market_id=market_id, outcome="YES", stake=10, user_id="other-user", idempotency_key="shared-key"
    )

    assert first["idempotent_replay"] is False and second["idempotent_replay"] is False
    assert len(list_positions(db_conn, DEMO_USER_ID)) == len(list_positions(db_conn, "other-user")) == 1


def test_concurrent_predictions_do_not_lose_balance_debits(tmp_path, sample_markets, monkeypatch):
    database = tmp_path / "concurrent-v1.sqlite3"
    setup = connect(str(database))
    init_db(setup)
    store_markets(setup, sample_markets)
    setup.close()

    original_get_balance = demo_points.get_balance
    pre_transaction_reads = Barrier(2)

    def synchronized_pre_fix_read(conn, user_id):
        balance = original_get_balance(conn, user_id)
        # Before the fix, V1 read balance outside a write transaction. This makes
        # the old lost-update interleaving deterministic without weakening it now.
        if not conn.in_transaction:
            pre_transaction_reads.wait(timeout=5)
        return balance

    monkeypatch.setattr(demo_points, "get_balance", synchronized_pre_fix_read)

    def predict(key):
        conn = connect(str(database))
        try:
            return create_demo_prediction(
                conn,
                market_id=sample_markets[0]["market_id"],
                outcome="YES",
                stake=100,
                idempotency_key=key,
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(predict, ("concurrent-a", "concurrent-b")))

    inspect = connect(str(database))
    try:
        assert [result["idempotent_replay"] for result in results] == [False, False]
        assert get_balance(inspect) == INITIAL_DEMO_POINTS - 200
        assert len(list_positions(inspect)) == 2
        assert len([entry for entry in list_ledger(inspect) if entry["entry_type"] == "prediction"]) == 2
    finally:
        inspect.close()


def test_concurrent_same_idempotency_key_creates_one_prediction(tmp_path, sample_markets, monkeypatch):
    database = tmp_path / "concurrent-idempotency-v1.sqlite3"
    setup = connect(str(database))
    init_db(setup)
    store_markets(setup, sample_markets)
    setup.close()

    original_get_balance = demo_points.get_balance
    pre_transaction_reads = Barrier(2)

    def synchronized_pre_fix_read(conn, user_id):
        balance = original_get_balance(conn, user_id)
        if not conn.in_transaction:
            pre_transaction_reads.wait(timeout=5)
        return balance

    monkeypatch.setattr(demo_points, "get_balance", synchronized_pre_fix_read)

    def predict():
        conn = connect(str(database))
        try:
            return create_demo_prediction(
                conn,
                market_id=sample_markets[0]["market_id"],
                outcome="YES",
                stake=100,
                idempotency_key="same-concurrent-key",
            )
        finally:
            conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: predict(), range(2)))

    inspect = connect(str(database))
    try:
        assert sorted(result["idempotent_replay"] for result in results) == [False, True]
        assert get_balance(inspect) == INITIAL_DEMO_POINTS - 100
        assert len(list_positions(inspect)) == 1
        assert len([entry for entry in list_ledger(inspect) if entry["entry_type"] == "prediction"]) == 1
    finally:
        inspect.close()


def test_prediction_failure_rolls_back_balance_and_related_v1_rows(db_conn, sample_markets, monkeypatch):
    def fail_settlement(*_args, **_kwargs):
        raise RuntimeError("forced settlement insert failure")

    monkeypatch.setattr(demo_points, "create_pending_settlement_for_position", fail_settlement)

    with pytest.raises(RuntimeError, match="forced settlement"):
        create_demo_prediction(db_conn, market_id=sample_markets[0]["market_id"], outcome="YES", stake=100)

    assert get_balance(db_conn) == INITIAL_DEMO_POINTS
    assert list_positions(db_conn) == []
    assert [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "prediction"] == []
    assert db_conn.execute("select count(*) from simulated_orders").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from demo_settlements").fetchone()[0] == 0
    assert db_conn.execute("select count(*) from demo_audit_events where event_type = 'demo_prediction_created'").fetchone()[0] == 0
