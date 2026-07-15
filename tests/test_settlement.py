import pytest

from app.demo_points import create_demo_prediction
from app.polymarket_gamma import MarketDetailResult
from app.settlement import compare_candidate_with_rest_resolution, extract_winning_outcome, settle_one, settle_pending_positions
from app.storage import (
    INITIAL_DEMO_POINTS,
    get_balance,
    get_market,
    get_settlement_by_position_id,
    insert_realtime_update,
    list_audit_events,
    list_ledger,
    replace_markets,
)


@pytest.fixture(autouse=True)
def fresh_rest_detail(db_conn, monkeypatch):
    """Settlement tests never contact the network; emulate the detail-only REST path."""
    import app.settlement as settlement_module

    def fetch(market_id):
        market = get_market(db_conn, market_id)
        payload = dict(market or {})
        payload["id"] = market_id
        payload["clobTokenIds"] = payload.get("clob_token_ids") or [f"{market_id}-yes", f"{market_id}-no"]
        if payload.get("closed") and payload.get("probabilities", {}).get("YES") == 1.0:
            payload["winningOutcome"] = "YES"
        return MarketDetailResult(True, "ok", payload, "2026-01-01T00:00:00+00:00", "mock://market")

    monkeypatch.setattr(settlement_module, "fetch_market_detail_for_settlement", fetch)


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


def test_compare_rest_clear_matching_ws_candidate_confirmed_match():
    market = {"closed": True, "outcomes": ["YES", "NO"], "probabilities": {"YES": 1.0, "NO": 0.0}}
    candidate = {"winning_outcome": "YES", "winning_asset_id": None}
    result = compare_candidate_with_rest_resolution(candidate, market)
    assert result["confirmation_status"] == "confirmed_match"


def test_compare_rest_clear_without_candidate():
    market = {"closed": True, "outcomes": ["YES", "NO"], "probabilities": {"YES": 1.0, "NO": 0.0}}
    result = compare_candidate_with_rest_resolution(None, market)
    assert result["confirmation_status"] == "rest_clear_without_candidate"


def test_compare_candidate_only_unconfirmed():
    market = {"closed": True, "outcomes": ["YES", "NO"], "probabilities": {"YES": 0.9, "NO": 0.1}}
    candidate = {"winning_outcome": "YES", "winning_asset_id": None}
    result = compare_candidate_with_rest_resolution(candidate, market)
    assert result["confirmation_status"] == "candidate_only_unconfirmed"


def test_compare_ws_candidate_conflict():
    market = {"closed": True, "outcomes": ["YES", "NO"], "probabilities": {"YES": 1.0, "NO": 0.0}}
    candidate = {"winning_outcome": "NO", "winning_asset_id": None}
    result = compare_candidate_with_rest_resolution(candidate, market)
    assert result["confirmation_status"] == "conflict"


def test_compare_both_unclear():
    market = {"closed": False, "outcomes": ["YES", "NO"], "probabilities": {"YES": 0.5, "NO": 0.5}}
    result = compare_candidate_with_rest_resolution(None, market)
    assert result["confirmation_status"] == "unclear"


def test_compare_candidate_asset_id_maps_only_when_clear():
    market = {
        "closed": True,
        "outcomes": ["YES", "NO"],
        "probabilities": {"YES": 1.0, "NO": 0.0},
        "clob_token_ids": ["asset-yes", "asset-no"],
    }
    clear = compare_candidate_with_rest_resolution({"winning_asset_id": "asset-yes"}, market)
    unclear = compare_candidate_with_rest_resolution({"winning_asset_id": "missing"}, market)
    assert clear["confirmation_status"] == "confirmed_match"
    assert unclear["confirmation_status"] == "conflict"


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


def test_confirmed_match_settles_win(db_conn, sample_markets):
    prediction, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    market = _resolved_market(sample_markets[0])
    replace_markets(db_conn, [market])
    insert_realtime_update(
        db_conn,
        {
            "market_id": market["market_id"],
            "asset_id": "asset-yes",
            "event_type": "market_resolved",
            "winning_outcome": "YES",
            "raw_event_json": "{}",
        },
    )
    db_conn.commit()
    result = settle_one(db_conn, int(settlement["id"]))
    assert result["status"] == "settled_win"
    assert result["evidence_status"] == "confirmed"
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100 + prediction["position"]["estimated_return"]


def test_settled_loss_does_not_increase_demo_balance(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets, outcome="NO", stake=100)
    replace_markets(db_conn, [_resolved_market(sample_markets[0])])

    result = settle_one(db_conn, int(settlement["id"]))

    assert result["status"] == "settled_loss"
    assert result["payout"] == 0
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100


def test_candidate_only_unconfirmed_does_not_settle_or_move_balance(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    market = _resolved_market(sample_markets[0], yes_probability=0.9, no_probability=0.1)
    replace_markets(db_conn, [market])
    insert_realtime_update(
        db_conn,
        {
            "market_id": market["market_id"],
            "asset_id": "asset-yes",
            "event_type": "market_resolved",
            "winning_outcome": "YES",
            "raw_event_json": "{}",
        },
    )
    db_conn.commit()
    result = settle_one(db_conn, int(settlement["id"]))
    assert result["status"] == "settlement_pending"
    assert result["evidence_status"] == "unresolved"
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100
    assert not [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "settlement_win"]


def test_conflict_does_not_settle_or_move_balance(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    market = _resolved_market(sample_markets[0])
    replace_markets(db_conn, [market])
    insert_realtime_update(
        db_conn,
        {
            "market_id": market["market_id"],
            "asset_id": "asset-no",
            "event_type": "market_resolved",
            "winning_outcome": "NO",
            "raw_event_json": "{}",
        },
    )
    db_conn.commit()
    result = settle_one(db_conn, int(settlement["id"]))
    assert result["status"] == "settled_win"
    assert result["evidence_status"] == "confirmed"
    assert get_balance(db_conn) > INITIAL_DEMO_POINTS - 100


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
    assert summary["rest_only_settled_count"] == 0


def test_settlement_audit_events_for_candidate_states(db_conn, sample_markets):
    _, settlement = _create_position(db_conn, sample_markets, outcome="YES", stake=100)
    market = _resolved_market(sample_markets[0], yes_probability=0.9, no_probability=0.1)
    replace_markets(db_conn, [market])
    insert_realtime_update(
        db_conn,
        {
            "market_id": market["market_id"],
            "asset_id": "asset-yes",
            "event_type": "market_resolved",
            "winning_outcome": "YES",
            "raw_event_json": "{}",
        },
    )
    db_conn.commit()
    settle_one(db_conn, int(settlement["id"]))
    event_types = {event["event_type"] for event in list_audit_events(db_conn)}
    assert "settlement_evidence_not_confirmed" in event_types
