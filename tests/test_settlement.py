from app.demo_points import create_demo_prediction
from app.settlement import extract_winning_outcome, settle_one, settle_pending_positions
from app.storage import (
    INITIAL_DEMO_POINTS,
    get_balance,
    get_settlement_by_position_id,
    list_ledger,
    replace_markets,
)


def _resolved_market(sample_market, *, yes_probability=1.0, no_probability=0.0, closed=True):
    market = dict(sample_market)
    market["closed"] = closed
    market["active"] = False
    market["probabilities"] = {"YES": yes_probability, "NO": no_probability}
    return market


def _create_position(db_conn, sample_markets, outcome="YES", stake=100):
    response = create_demo_prediction(
        db_conn,
        market_id=sample_markets[0]["market_id"],
        outcome=outcome,
        stake=stake,
    )
    settlement = get_settlement_by_position_id(db_conn, int(response["position"]["id"]))
    return response, settlement


def test_extract_winning_outcome_explicit_snake_case():
    assert extract_winning_outcome({"winning_outcome": "YES"}) == "YES"


def test_extract_winning_outcome_explicit_camel_case():
    assert extract_winning_outcome({"winningOutcome": "NO"}) == "NO"


def test_extract_winning_outcome_maps_asset_id_when_clear():
    market = {
        "outcomes": ["YES", "NO"],
        "clob_token_ids": ["token-yes", "token-no"],
        "winningAssetId": "token-no",
    }
    assert extract_winning_outcome(market) == "NO"


def test_extract_winning_outcome_conservative_probability_fallback():
    market = {"closed": True, "outcomes": ["YES", "NO"], "probabilities": {"YES": 1.0, "NO": 0.0}}
    assert extract_winning_outcome(market) == "YES"


def test_extract_winning_outcome_does_not_infer_from_high_probability():
    market = {"closed": True, "outcomes": ["YES", "NO"], "probabilities": {"YES": 0.9, "NO": 0.1}}
    assert extract_winning_outcome(market) is None


def test_no_settlement_when_outcome_unclear(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets)
    unclear = dict(sample_markets[0])
    unclear["closed"] = True
    unclear["probabilities"] = {"YES": 0.9, "NO": 0.1}
    replace_markets(db_conn, [unclear])

    result = settle_one(db_conn, int(settlement["id"]))

    assert result["status"] == "settlement_pending"
    assert result["payout"] == 0
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100


def test_settled_win_increases_demo_balance(db_conn, sample_markets):
    prediction, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    replace_markets(db_conn, [_resolved_market(sample_markets[0])])

    result = settle_one(db_conn, int(settlement["id"]))

    assert result["status"] == "settled_win"
    assert result["winning_outcome"] == "YES"
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100 + prediction["position"]["estimated_return"]


def test_settled_loss_does_not_increase_demo_balance(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets, outcome="NO", stake=100)
    replace_markets(db_conn, [_resolved_market(sample_markets[0])])

    result = settle_one(db_conn, int(settlement["id"]))

    assert result["status"] == "settled_loss"
    assert result["payout"] == 0
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100


def test_repeated_settlement_call_does_not_double_pay(db_conn, sample_markets):
    prediction, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    replace_markets(db_conn, [_resolved_market(sample_markets[0])])

    settle_one(db_conn, int(settlement["id"]))
    balance_after_first = get_balance(db_conn)
    settle_one(db_conn, int(settlement["id"]))

    assert balance_after_first == INITIAL_DEMO_POINTS - 100 + prediction["position"]["estimated_return"]
    assert get_balance(db_conn) == balance_after_first


def test_repeated_settlement_call_does_not_duplicate_ledger_payout(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    replace_markets(db_conn, [_resolved_market(sample_markets[0])])

    settle_one(db_conn, int(settlement["id"]))
    settle_one(db_conn, int(settlement["id"]))

    payout_entries = [
        entry for entry in list_ledger(db_conn) if f"settlement_id={settlement['id']}" in entry["note"]
    ]
    assert len(payout_entries) == 1


def test_settle_pending_positions_summary(db_conn, sample_markets):
    _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    _create_position(db_conn, sample_markets, outcome="NO", stake=100)
    replace_markets(db_conn, [_resolved_market(sample_markets[0])])

    summary = settle_pending_positions(db_conn)

    assert summary["checked_count"] == 2
    assert summary["settled_win_count"] == 1
    assert summary["settled_loss_count"] == 1
    assert summary["pending_count"] == 0
