from datetime import datetime, timedelta, timezone
import json

from app.config import Settings
from app.polymarket_ws import (
    apply_ws_events_to_storage,
    build_market_subscription,
    extract_asset_ids_from_market,
    parse_ws_event,
    select_ws_markets,
)
from app.realtime import attach_realtime_updates, realtime_status
from app.storage import get_latest_realtime_update, insert_realtime_update, replace_markets


def _settings(stale_seconds=90):
    return Settings(
        live=False,
        poll_seconds=30,
        limit=50,
        db_path=":memory:",
        ws_enabled=True,
        ws_top_n=10,
        ws_stale_seconds=stale_seconds,
    )


def _ws_market(sample_market, market_id="ws-market", asset_ids=None, volume_24hr=100, liquidity=100):
    market = dict(sample_market)
    market["market_id"] = market_id
    market["clob_token_ids"] = asset_ids or ["asset-yes", "asset-no"]
    market["volume_24hr"] = volume_24hr
    market["liquidity"] = liquidity
    market["active"] = True
    market["closed"] = False
    market["end_date"] = "2099-01-01T00:00:00+00:00"
    market["probabilities"] = {"YES": 0.5, "NO": 0.5}
    return market


def test_extract_asset_ids_from_market_supports_list_and_json(sample_markets):
    assert extract_asset_ids_from_market({"clob_token_ids": ["a", "b"]}) == ["a", "b"]
    assert extract_asset_ids_from_market({"clobTokenIds": json.dumps(["x", "y"])}) == ["x", "y"]


def test_select_ws_markets_prefers_displayable_liquid_markets(sample_markets):
    low = _ws_market(sample_markets[0], market_id="low", volume_24hr=1, liquidity=1)
    high = _ws_market(sample_markets[0], market_id="high", volume_24hr=100, liquidity=50)
    closed = _ws_market(sample_markets[0], market_id="closed", volume_24hr=1000, liquidity=1000)
    closed["closed"] = True
    selected = select_ws_markets([low, closed, high], top_n=2)
    assert [market["market_id"] for market in selected] == ["high", "low"]


def test_build_market_subscription_shape():
    assert build_market_subscription(["a", "b"]) == {
        "assets_ids": ["a", "b"],
        "type": "market",
        "custom_feature_enabled": True,
    }


def test_parse_ws_event_handles_best_bid_ask():
    events = parse_ws_event({"event_type": "best_bid_ask", "asset_id": "asset-yes", "best_bid": "0.42", "best_ask": "0.44"})
    assert events[0]["event_type"] == "best_bid_ask"
    assert events[0]["best_bid"] == 0.42
    assert events[0]["best_ask"] == 0.44
    assert events[0]["spread"] == 0.02


def test_parse_ws_event_handles_last_trade_price():
    events = parse_ws_event('{"event_type":"last_trade_price","asset_id":"asset-yes","last_trade_price":"0.61"}')
    assert events[0]["last_trade_price"] == 0.61


def test_parse_ws_event_handles_price_change():
    events = parse_ws_event(
        {
            "event_type": "price_change",
            "changes": [
                {"asset_id": "asset-yes", "price": "0.51", "size": "10"},
                {"asset_id": "asset-no", "price": "0.49", "side": "ASK"},
            ],
        }
    )
    assert len(events) == 2
    assert events[0]["price"] == 0.51
    assert events[1]["side"] == "ASK"


def test_parse_ws_event_handles_market_resolved():
    events = parse_ws_event({"event_type": "market_resolved", "asset_id": "asset-yes", "winningOutcome": "YES"})
    assert events[0]["winning_outcome"] == "YES"
    assert events[0]["winning_asset_id"] == "asset-yes"


def test_malformed_websocket_messages_are_ignored_safely():
    assert parse_ws_event("not-json") == []
    assert parse_ws_event({"event_type": "unknown"}) == []


def test_apply_ws_events_to_storage_stores_updates(db_conn, sample_markets):
    market = _ws_market(sample_markets[0])
    replace_markets(db_conn, [market])
    events = parse_ws_event({"event_type": "best_bid_ask", "asset_id": "asset-yes", "best_bid": 0.4, "best_ask": 0.5})

    inserted = apply_ws_events_to_storage(db_conn, [market], events)
    latest = get_latest_realtime_update(db_conn, market["market_id"])

    assert inserted == 1
    assert latest["market_id"] == market["market_id"]
    assert latest["best_bid"] == 0.4


def test_realtime_status_returns_rest_only_when_no_updates(db_conn):
    status = realtime_status(db_conn, _settings())
    assert status["rest_only_count"] >= 1
    assert status["live_market_update_count"] == 0


def test_realtime_status_returns_ws_live_when_fresh_update_exists(db_conn, sample_markets):
    market = _ws_market(sample_markets[0])
    replace_markets(db_conn, [market])
    insert_realtime_update(
        db_conn,
        {
            "market_id": market["market_id"],
            "asset_id": "asset-yes",
            "event_type": "best_bid_ask",
            "best_bid": 0.4,
            "best_ask": 0.5,
            "raw_event_json": "{}",
            "event_timestamp": datetime.now(timezone.utc).isoformat(),
        },
    )
    db_conn.commit()
    status = realtime_status(db_conn, _settings(stale_seconds=90))
    assert status["live_market_update_count"] == 1
    assert status["stale_market_update_count"] == 0


def test_realtime_status_returns_ws_stale_when_update_is_old(db_conn, sample_markets):
    market = _ws_market(sample_markets[0])
    replace_markets(db_conn, [market])
    insert_realtime_update(
        db_conn,
        {
            "market_id": market["market_id"],
            "asset_id": "asset-yes",
            "event_type": "best_bid_ask",
            "best_bid": 0.4,
            "best_ask": 0.5,
            "raw_event_json": "{}",
            "event_timestamp": (datetime.now(timezone.utc) - timedelta(seconds=900)).isoformat(),
        },
    )
    db_conn.commit()
    enriched = attach_realtime_updates(db_conn, [market], _settings(stale_seconds=15))
    status = realtime_status(db_conn, _settings(stale_seconds=15))
    assert enriched[0]["realtime_status"] == "ws_stale"
    assert status["stale_market_update_count"] == 1
