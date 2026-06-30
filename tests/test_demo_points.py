from app.demo_points import DemoPredictionError, create_demo_prediction
from app.storage import INITIAL_DEMO_POINTS, get_balance, list_ledger, list_positions


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
