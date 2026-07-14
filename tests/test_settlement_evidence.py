import json

from app.demo_points import create_demo_prediction
from app.polymarket_gamma import MarketDetailResult
from app.settlement import settle_one
from app.settlement_evidence import evidence_hash, normalize_rest_evidence, validate_settlement_evidence
from app.storage import INITIAL_DEMO_POINTS, get_balance, get_settlement_by_position_id, list_ledger, list_settlement_evidence


def detail(**overrides):
    item = {"id": "market-1", "closed": True, "resolved": True, "outcomes": ["YES", "NO"],
            "clobTokenIds": ["asset-yes", "asset-no"], "winningOutcome": "YES", "winningAssetId": "asset-yes",
            "outcomePrices": ["1", "0"]}
    item.update(overrides)
    return item


def test_normalization_hash_is_stable_and_secret_free():
    first = normalize_rest_evidence("market-1", detail(api_key="do-not-store", headers={"Authorization": "no"}), fetched_at="2026-01-01T00:00:00+00:00")
    second = normalize_rest_evidence("market-1", detail(headers={"x": "different"}, api_key="other"), fetched_at="2026-01-01T00:00:00+00:00")
    assert evidence_hash(first) == evidence_hash(second)
    assert "api_key" not in json.dumps(first)
    assert validate_settlement_evidence(first)["status"] == "confirmed"


def test_validation_rejects_probability_only_and_conflicting_winners():
    probability_only = normalize_rest_evidence("market-1", detail(winningOutcome=None, winningAssetId=None))
    assert validate_settlement_evidence(probability_only)["status"] == "unresolved"
    conflicting = normalize_rest_evidence("market-1", detail(winningOutcome="YES", winningAssetId="asset-no"))
    assert validate_settlement_evidence(conflicting)["status"] == "conflict"


def test_validation_rejects_bad_mapping():
    bad = normalize_rest_evidence("market-1", detail(clobTokenIds=["asset-yes"]))
    assert "outcome_token_length_mismatch" in validate_settlement_evidence(bad)["failure_codes"]
    duplicate = normalize_rest_evidence("market-1", detail(clobTokenIds=["asset-yes", "asset-yes"]))
    assert "duplicate_token_id" in validate_settlement_evidence(duplicate)["failure_codes"]


def test_unavailable_evidence_never_credits_or_settles(db_conn, sample_markets):
    created = create_demo_prediction(db_conn, market_id=sample_markets[0]["market_id"], outcome="YES", stake=100)
    row = get_settlement_by_position_id(db_conn, created["position"]["id"])
    result = settle_one(db_conn, row["id"], market_detail_fetcher=lambda _: MarketDetailResult(False, "unavailable", None, "2026-01-01T00:00:00+00:00", "mock://", "timeout"))
    assert result["evidence_status"] == "unavailable"
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 100
    assert not [x for x in list_ledger(db_conn) if x["entry_type"].startswith("settlement")]
    assert list_settlement_evidence(db_conn, row["market_id"])[-1]["validation_status"] == "unavailable"


def test_confirmed_evidence_records_hash_and_is_idempotent(db_conn, sample_markets):
    created = create_demo_prediction(db_conn, market_id=sample_markets[0]["market_id"], outcome="YES", stake=100)
    row = get_settlement_by_position_id(db_conn, created["position"]["id"])
    fetch = lambda market_id: MarketDetailResult(True, "ok", detail(id=market_id), "2026-01-01T00:00:00+00:00", "mock://")
    result = settle_one(db_conn, row["id"], market_detail_fetcher=fetch)
    again = settle_one(db_conn, row["id"], market_detail_fetcher=fetch)
    assert result["evidence_status"] == "confirmed"
    assert again["status"] == "settled_win"
    assert len([x for x in list_ledger(db_conn) if x["entry_type"] == "settlement_win"]) == 1
    assert list_settlement_evidence(db_conn, row["market_id"])[-1]["evidence_hash"] == result["evidence_hash"]
