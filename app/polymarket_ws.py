from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import json
import sqlite3
from typing import Any

from app.market_display import classify_market_for_display
from app.storage import insert_realtime_update

PUBLIC_MARKET_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _float(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _event_time(event: dict[str, Any]) -> str | None:
    value = _first(event.get("timestamp"), event.get("event_timestamp"), event.get("time"), event.get("created_at"))
    if value in (None, ""):
        return None
    if isinstance(value, (int, float)):
        timestamp = float(value)
        if timestamp > 10_000_000_000:
            timestamp = timestamp / 1000
        return datetime.fromtimestamp(timestamp, timezone.utc).isoformat()
    return str(value)


def _price_from_level(level: Any) -> float | None:
    if isinstance(level, dict):
        return _float(_first(level.get("price"), level.get("p")))
    if isinstance(level, (list, tuple)) and level:
        return _float(level[0])
    return _float(level)


def _best_bid_ask(event: dict[str, Any]) -> tuple[float | None, float | None]:
    best_bid = _float(_first(event.get("best_bid"), event.get("bestBid"), event.get("bid")))
    best_ask = _float(_first(event.get("best_ask"), event.get("bestAsk"), event.get("ask")))
    bids = event.get("bids") or event.get("buys")
    asks = event.get("asks") or event.get("sells")
    if best_bid is None and isinstance(bids, list):
        bid_prices = [price for price in (_price_from_level(level) for level in bids) if price is not None]
        best_bid = max(bid_prices) if bid_prices else None
    if best_ask is None and isinstance(asks, list):
        ask_prices = [price for price in (_price_from_level(level) for level in asks) if price is not None]
        best_ask = min(ask_prices) if ask_prices else None
    return best_bid, best_ask


def extract_asset_ids_from_market(market: dict[str, Any]) -> list[str]:
    keys = ("clobTokenIds", "clob_token_ids", "tokenIds", "token_ids", "assetIds", "asset_ids")
    for key in keys:
        value = _parse_jsonish(market.get(key))
        if isinstance(value, list):
            return [str(item).strip() for item in value if str(item).strip()]

    tokens = _parse_jsonish(market.get("tokens"))
    if isinstance(tokens, list):
        ids: list[str] = []
        for token in tokens:
            if not isinstance(token, dict):
                continue
            token_id = _first(token.get("id"), token.get("token_id"), token.get("tokenId"), token.get("asset_id"))
            if token_id:
                ids.append(str(token_id))
        return ids
    return []


def select_ws_markets(markets: list[dict[str, Any]], top_n: int) -> list[dict[str, Any]]:
    with_assets = [market for market in markets if extract_asset_ids_from_market(market)]

    def sort_key(market: dict[str, Any]) -> tuple[int, float, float]:
        classification = classify_market_for_display(market)
        return (
            1 if classification.is_displayable else 0,
            _float(market.get("volume_24hr")) or 0.0,
            _float(market.get("liquidity")) or 0.0,
        )

    return sorted(with_assets, key=sort_key, reverse=True)[: max(0, top_n)]


def build_market_subscription(asset_ids: list[str]) -> dict[str, Any]:
    return {
        "assets_ids": [str(asset_id) for asset_id in asset_ids],
        "type": "market",
        "custom_feature_enabled": True,
    }


def _normalize_event(event: dict[str, Any]) -> dict[str, Any] | None:
    event_type = str(_first(event.get("event_type"), event.get("type"), event.get("event")) or "").strip()
    if event_type not in {"book", "price_change", "last_trade_price", "best_bid_ask", "market_resolved"}:
        return None

    asset_id = _first(event.get("asset_id"), event.get("assetId"), event.get("asset"), event.get("token_id"))
    best_bid, best_ask = _best_bid_ask(event)
    spread = None
    if best_bid is not None and best_ask is not None:
        spread = round(best_ask - best_bid, 6)

    return {
        "event_type": event_type,
        "asset_id": str(asset_id) if asset_id else None,
        "best_bid": best_bid,
        "best_ask": best_ask,
        "last_trade_price": _float(_first(event.get("last_trade_price"), event.get("lastTradePrice"))),
        "price": _float(event.get("price")),
        "size": _float(event.get("size")),
        "side": str(event.get("side")) if event.get("side") else None,
        "spread": spread,
        "winning_outcome": _first(event.get("winning_outcome"), event.get("winningOutcome")),
        "winning_asset_id": _first(event.get("winning_asset_id"), event.get("winningAssetId"), asset_id if event_type == "market_resolved" else None),
        "raw_event_json": json.dumps(event, ensure_ascii=False),
        "event_timestamp": _event_time(event),
    }


def parse_ws_event(message: str | dict | list) -> list[dict[str, Any]]:
    payload = _parse_jsonish(message)
    if isinstance(payload, dict):
        if isinstance(payload.get("changes"), list) and payload.get("event_type") == "price_change":
            events = []
            for change in payload["changes"]:
                if isinstance(change, dict):
                    merged = dict(payload)
                    merged.pop("changes", None)
                    merged.update(change)
                    events.append(merged)
        else:
            events = [payload]
    elif isinstance(payload, list):
        events = [item for item in payload if isinstance(item, dict)]
    else:
        return []

    normalized: list[dict[str, Any]] = []
    for event in events:
        parsed = _normalize_event(event)
        if parsed:
            normalized.append(parsed)
    return normalized


def map_asset_to_market(markets: list[dict[str, Any]], asset_id: str | None) -> str | None:
    if not asset_id:
        return None
    for market in markets:
        if str(asset_id) in extract_asset_ids_from_market(market):
            return str(market.get("market_id"))
    return None


def apply_ws_events_to_storage(conn: sqlite3.Connection, markets: list[dict[str, Any]], events: list[dict[str, Any]]) -> int:
    inserted = 0
    with conn:
        for event in events:
            update = dict(event)
            update["market_id"] = map_asset_to_market(markets, event.get("asset_id") or event.get("winning_asset_id"))
            insert_realtime_update(conn, update)
            inserted += 1
    return inserted


async def run_market_ws_once(conn: sqlite3.Connection, markets: list[dict[str, Any]], *, top_n: int = 10, timeout: float = 30.0) -> int:
    selected = select_ws_markets(markets, top_n)
    asset_ids = [asset_id for market in selected for asset_id in extract_asset_ids_from_market(market)]
    if not asset_ids:
        return 0
    try:
        import websockets
    except ImportError as exc:
        raise RuntimeError("websockets package is required for optional market WebSocket mode") from exc

    inserted = 0
    async with websockets.connect(PUBLIC_MARKET_WS_URL, ping_interval=10, open_timeout=10) as websocket:
        await websocket.send(json.dumps(build_market_subscription(asset_ids)))
        message = await asyncio.wait_for(websocket.recv(), timeout=timeout)
        inserted += apply_ws_events_to_storage(conn, markets, parse_ws_event(message))
    return inserted
