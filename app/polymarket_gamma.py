from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

import httpx

GAMMA_EVENTS_URL = "https://gamma-api.polymarket.com/events"
SAMPLE_PATH = Path("data/sample_events.json")


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def _parse_jsonish(value: Any) -> Any:
    if isinstance(value, str):
        try:
            return json.loads(value)
        except json.JSONDecodeError:
            return value
    return value


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value is None or value == "":
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _first(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default


def load_sample_events(path: Path | str = SAMPLE_PATH) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    if isinstance(data, dict):
        return list(data.get("events", []))
    return list(data)


def _event_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = event.get("markets")
    if isinstance(markets, list) and markets:
        return [m for m in markets if isinstance(m, dict)]
    return [event]


def _normalize_outcomes(market: dict[str, Any]) -> tuple[list[str], dict[str, float]]:
    raw_outcomes = _parse_jsonish(_first(market.get("outcomes"), market.get("tokens"), default=[]))
    raw_prices = _parse_jsonish(
        _first(
            market.get("outcomePrices"),
            market.get("outcome_prices"),
            market.get("probabilities"),
            default=[],
        )
    )
    outcomes: list[str] = []
    probabilities: dict[str, float] = {}

    if isinstance(raw_outcomes, list):
        for item in raw_outcomes:
            if isinstance(item, dict):
                label = str(_first(item.get("name"), item.get("outcome"), item.get("label"), default="")).strip()
                price = _first(item.get("price"), item.get("probability"), item.get("lastPrice"))
            else:
                label = str(item).strip()
                price = None
            if label:
                outcomes.append(label)
                if price is not None:
                    probabilities[label] = _float(price)

    if isinstance(raw_prices, dict):
        for label, price in raw_prices.items():
            label_str = str(label)
            if label_str not in outcomes:
                outcomes.append(label_str)
            probabilities[label_str] = _float(price)
    elif isinstance(raw_prices, list):
        for index, price in enumerate(raw_prices):
            if index < len(outcomes):
                probabilities[outcomes[index]] = _float(price)

    if not outcomes:
        outcomes = ["YES", "NO"]
    if not probabilities:
        default = round(1 / len(outcomes), 4)
        probabilities = {label: default for label in outcomes}

    return outcomes, {label: max(0.0, min(1.0, probabilities.get(label, 0.0))) for label in outcomes}


def normalize_events(
    events: list[dict[str, Any]],
    *,
    source: str,
    status: str,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    fetched_at = fetched_at or utc_now_iso()
    normalized: list[dict[str, Any]] = []
    for event in events:
        event_id = str(_first(event.get("id"), event.get("event_id"), event.get("external_event_id"), default=""))
        event_title = _first(event.get("title"), event.get("name"), event.get("question"), default="Prediction market")
        for market in _event_markets(event):
            market_id = str(_first(market.get("id"), market.get("market_id"), market.get("conditionId"), default=event_id))
            outcomes, probabilities = _normalize_outcomes(market)
            question = _first(market.get("question"), event.get("question"), event_title)
            title = _first(event_title, question)
            slug = str(_first(market.get("slug"), event.get("slug"), market_id, default=market_id))
            normalized.append(
                {
                    "market_id": market_id,
                    "source": source,
                    "external_event_id": event_id,
                    "external_market_id": market_id,
                    "slug": slug,
                    "title": str(title),
                    "question": str(question),
                    "description": str(_first(market.get("description"), event.get("description"), market.get("resolution_condition"), default="")),
                    "outcomes": outcomes,
                    "probabilities": probabilities,
                    "volume": _float(_first(market.get("volume"), event.get("volume"), default=0)),
                    "volume_24hr": _float(_first(market.get("volume_24hr"), market.get("volume24hr"), event.get("volume_24hr"), event.get("volume24hr"), default=0)),
                    "liquidity": _float(_first(market.get("liquidity"), event.get("liquidity"), default=0)),
                    "active": bool(_first(market.get("active"), event.get("active"), default=True)),
                    "closed": bool(_first(market.get("closed"), event.get("closed"), default=False)),
                    "end_date": str(_first(market.get("end_date"), market.get("endDate"), event.get("end_date"), event.get("endDate"), default="")),
                    "fetched_at": fetched_at,
                    "data_source_status": status,
                }
            )
    return normalized


def fetch_live_events(limit: int = 50, timeout: float = 5.0) -> list[dict[str, Any]]:
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume_24hr",
        "ascending": "false",
        "limit": str(max(limit, 100)),
    }
    response = httpx.get(GAMMA_EVENTS_URL, params=params, timeout=timeout)
    response.raise_for_status()
    data = response.json()
    if isinstance(data, dict):
        return list(data.get("events") or data.get("data") or [])
    return list(data)


def load_markets(*, live: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    fetched_at = utc_now_iso()
    if live:
        try:
            events = fetch_live_events(limit=limit)
            markets = normalize_events(events, source="polymarket_gamma", status="live", fetched_at=fetched_at)
            if markets:
                return markets[:limit]
        except Exception:
            events = load_sample_events()
            return normalize_events(events, source="sample", status="last fetch failed", fetched_at=fetched_at)[:limit]
    events = load_sample_events()
    return normalize_events(events, source="sample", status="sample fallback", fetched_at=fetched_at)[:limit]
