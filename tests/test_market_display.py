from copy import deepcopy

from app.demo_points import DemoPredictionError, create_demo_prediction
from app.market_display import classify_market_for_display, filtered_market_response
from app.storage import get_balance, replace_markets


def _variant(market, market_id, **changes):
    clone = deepcopy(market)
    clone["market_id"] = market_id
    clone["external_market_id"] = market_id
    clone.update(changes)
    return clone


def test_demo_participation_allowed_for_valid_active_market(sample_markets):
    classification = classify_market_for_display(sample_markets[0])
    assert classification.is_displayable is True
    assert classification.is_demo_participation_allowed is True


def test_closed_inactive_expired_markets_hidden_by_default(sample_markets):
    base = sample_markets[0]
    markets = [
        base,
        _variant(base, "closed-market", closed=True),
        _variant(base, "inactive-market", active=False),
        _variant(base, "expired-market", end_date="2000-01-01T00:00:00Z"),
    ]
    response = filtered_market_response(markets)
    ids = {market["market_id"] for market in response["markets"]}
    assert ids == {base["market_id"]}
    assert response["hidden_closed_count"] == 1
    assert response["hidden_inactive_count"] == 1
    assert response["hidden_expired_count"] == 1


def test_include_all_returns_all_markets(sample_markets):
    base = sample_markets[0]
    markets = [base, _variant(base, "closed-market", closed=True)]
    response = filtered_market_response(markets, include_all=True)
    assert response["count"] == 2
    assert response["total_market_count"] == 2
    assert response["displayable_market_count"] == 1


def test_hidden_no_liquidity_and_resolved_counts(sample_markets):
    base = sample_markets[0]
    markets = [
        base,
        _variant(base, "no-liquidity-market", liquidity=0),
        _variant(base, "resolved-market", probabilities={"YES": 1.0, "NO": 0.0}),
    ]
    response = filtered_market_response(markets)
    assert response["count"] == 1
    assert response["hidden_no_liquidity_count"] == 1
    assert response["hidden_resolved_probability_count"] == 1


def test_demo_prediction_rejected_for_closed_market(db_conn, sample_markets):
    market = _variant(sample_markets[0], "closed-market", closed=True)
    replace_markets(db_conn, [market])
    try:
        create_demo_prediction(db_conn, market_id="closed-market", outcome="YES", stake=10)
    except DemoPredictionError as exc:
        assert "終了済み" in str(exc)
    else:
        raise AssertionError("expected closed market rejection")


def test_demo_prediction_rejected_for_expired_market(db_conn, sample_markets):
    market = _variant(sample_markets[0], "expired-market", end_date="2000-01-01T00:00:00Z")
    replace_markets(db_conn, [market])
    try:
        create_demo_prediction(db_conn, market_id="expired-market", outcome="YES", stake=10)
    except DemoPredictionError as exc:
        assert "期限切れ" in str(exc)
    else:
        raise AssertionError("expected expired market rejection")


def test_demo_prediction_rejected_for_inactive_market(db_conn, sample_markets):
    market = _variant(sample_markets[0], "inactive-market", active=False)
    replace_markets(db_conn, [market])
    try:
        create_demo_prediction(db_conn, market_id="inactive-market", outcome="YES", stake=10)
    except DemoPredictionError as exc:
        assert "非アクティブ" in str(exc)
    else:
        raise AssertionError("expected inactive market rejection")


def test_demo_prediction_rejected_for_zero_liquidity_market(db_conn, sample_markets):
    market = _variant(sample_markets[0], "no-liquidity-market", liquidity=0)
    replace_markets(db_conn, [market])
    try:
        create_demo_prediction(db_conn, market_id="no-liquidity-market", outcome="YES", stake=10)
    except DemoPredictionError as exc:
        assert "流動性なし" in str(exc)
    else:
        raise AssertionError("expected no-liquidity market rejection")


def test_successful_demo_prediction_still_allowed_for_valid_market(db_conn, sample_markets):
    market = sample_markets[0]
    replace_markets(db_conn, [market])
    balance_before = get_balance(db_conn)
    result = create_demo_prediction(db_conn, market_id=market["market_id"], outcome="YES", stake=10)
    assert result["balance"] == balance_before - 10
