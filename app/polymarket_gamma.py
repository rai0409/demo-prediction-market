from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any
from urllib.parse import urlencode

import httpx

GAMMA_EVENTS_BASE_URL = "https://gamma-api.polymarket.com/events"
SAMPLE_PATH = Path("data/sample_events.json")
RUNTIME_DIR = Path("runtime")
GAMMA_RESPONSE_PATH = RUNTIME_DIR / "gamma_last_response.json"
GAMMA_ERROR_PATH = RUNTIME_DIR / "gamma_last_error.txt"
GAMMA_STATUS_PATH = RUNTIME_DIR / "gamma_last_status.json"


@dataclass
class FetchResult:
    ok: bool
    status: str
    error: str | None
    raw_count: int
    normalized_count: int
    markets: list[dict[str, Any]]
    attempted_at: str
    url: str
    http_status: int | None = None
    fallback_used: bool = False

    def as_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "status": self.status,
            "error": self.error,
            "raw_count": self.raw_count,
            "normalized_count": self.normalized_count,
            "markets": self.markets,
            "attempted_at": self.attempted_at,
            "url": self.url,
            "http_status": self.http_status,
            "fallback_used": self.fallback_used,
        }


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


def gamma_events_url(limit: int = 100) -> str:
    params = {
        "active": "true",
        "closed": "false",
        "order": "volume_24hr",
        "ascending": "false",
        "limit": str(max(limit, 100)),
    }
    return f"{GAMMA_EVENTS_BASE_URL}?{urlencode(params)}"


def _write_text(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_json(path: Path, data: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def write_status_file(result: FetchResult, *, live_enabled: bool) -> None:
    _write_json(
        GAMMA_STATUS_PATH,
        {
            "attempted_at": result.attempted_at,
            "live_enabled": live_enabled,
            "url": result.url,
            "http_status": result.http_status,
            "raw_count": result.raw_count,
            "normalized_count": result.normalized_count,
            "fallback_used": result.fallback_used,
            "error": result.error,
            "status": result.status,
        },
    )


def read_status_file() -> dict[str, Any] | None:
    if not GAMMA_STATUS_PATH.exists():
        return None
    try:
        return json.loads(GAMMA_STATUS_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return None


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


def _bool(value: Any, default: bool) -> bool:
    if value is None or value == "":
        return default
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _first(*values: Any, default: Any = None) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return default


def _extract_items(payload: Any) -> list[dict[str, Any]]:
    payload = _parse_jsonish(payload)
    if isinstance(payload, list):
        return [item for item in payload if isinstance(item, dict)]
    if isinstance(payload, dict):
        if isinstance(payload.get("events"), list):
            return [item for item in payload["events"] if isinstance(item, dict)]
        if isinstance(payload.get("data"), list):
            return [item for item in payload["data"] if isinstance(item, dict)]
        return [payload]
    return []


def load_sample_events(path: Path | str = SAMPLE_PATH) -> list[dict[str, Any]]:
    with Path(path).open("r", encoding="utf-8") as handle:
        data = json.load(handle)
    return _extract_items(data)


def _event_markets(event: dict[str, Any]) -> list[dict[str, Any]]:
    markets = _parse_jsonish(event.get("markets"))
    if isinstance(markets, list) and markets:
        return [m for m in markets if isinstance(m, dict)]
    return [event]


def _normalize_outcomes(market: dict[str, Any]) -> tuple[list[str], dict[str, float]]:
    raw_outcomes = _parse_jsonish(
        _first(market.get("outcomes"), market.get("tokens"), market.get("shortOutcomes"), default=[])
    )
    raw_prices = _parse_jsonish(
        _first(
            market.get("outcomePrices"),
            market.get("outcome_prices"),
            market.get("probabilities"),
            market.get("lastPrices"),
            default=[],
        )
    )
    _parse_jsonish(market.get("clobTokenIds"))
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

    if not outcomes and raw_prices:
        outcomes = ["YES", "NO"][: len(raw_prices)] if isinstance(raw_prices, list) else ["YES", "NO"]
    if not outcomes:
        outcomes = ["YES", "NO"]
    if not probabilities:
        default = round(1 / len(outcomes), 4)
        probabilities = {label: default for label in outcomes}

    return outcomes, {label: max(0.0, min(1.0, probabilities.get(label, 0.0))) for label in outcomes}


def normalize_events(
    payload: Any,
    *,
    source: str,
    status: str,
    fetched_at: str | None = None,
) -> list[dict[str, Any]]:
    fetched_at = fetched_at or utc_now_iso()
    normalized: list[dict[str, Any]] = []
    for event in _extract_items(payload):
        event_id = str(_first(event.get("id"), event.get("event_id"), event.get("external_event_id"), default=""))
        event_title = _first(event.get("title"), event.get("name"), event.get("question"), default="Prediction market")
        for market in _event_markets(event):
            market_id = str(
                _first(
                    market.get("id"),
                    market.get("market_id"),
                    market.get("conditionId"),
                    market.get("condition_id"),
                    event_id,
                    default="",
                )
            )
            question = _first(market.get("question"), event.get("question"), event_title)
            if not market_id or not question:
                continue
            outcomes, probabilities = _normalize_outcomes(market)
            if not outcomes:
                continue
            clob_token_ids = _parse_jsonish(
                _first(
                    market.get("clobTokenIds"),
                    market.get("clob_token_ids"),
                    market.get("tokenIds"),
                    market.get("outcomeTokenIds"),
                    default=[],
                )
            )
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
                    "description": str(
                        _first(
                            market.get("description"),
                            event.get("description"),
                            market.get("resolutionSource"),
                            market.get("resolution_condition"),
                            default="",
                        )
                    ),
                    "outcomes": outcomes,
                    "probabilities": probabilities,
                    "clob_token_ids": clob_token_ids if isinstance(clob_token_ids, list) else [],
                    "winning_outcome": _first(
                        market.get("winning_outcome"),
                        market.get("winningOutcome"),
                        market.get("resolved_outcome"),
                        market.get("resolution_outcome"),
                        market.get("winningOutcomeName"),
                        event.get("winning_outcome"),
                        event.get("winningOutcome"),
                        event.get("resolved_outcome"),
                        event.get("resolution_outcome"),
                        event.get("winningOutcomeName"),
                    ),
                    "winning_asset_id": _first(
                        market.get("winning_asset_id"),
                        market.get("winningAssetId"),
                        event.get("winning_asset_id"),
                        event.get("winningAssetId"),
                    ),
                    "resolved": _bool(
                        _first(
                            market.get("resolved"),
                            market.get("market_resolved"),
                            market.get("marketResolved"),
                            event.get("resolved"),
                            event.get("market_resolved"),
                            event.get("marketResolved"),
                            default=False,
                        ),
                        False,
                    ),
                    "volume": _float(_first(market.get("volume"), event.get("volume"), default=0)),
                    "volume_24hr": _float(
                        _first(
                            market.get("volume_24hr"),
                            market.get("volume24hr"),
                            market.get("volume24Hr"),
                            event.get("volume_24hr"),
                            event.get("volume24hr"),
                            event.get("volume24Hr"),
                            default=0,
                        )
                    ),
                    "liquidity": _float(
                        _first(market.get("liquidity"), market.get("liquidityNum"), event.get("liquidity"), default=0)
                    ),
                    "active": _bool(_first(market.get("active"), event.get("active"), default=True), True),
                    "closed": _bool(_first(market.get("closed"), event.get("closed"), default=False), False),
                    "end_date": str(
                        _first(
                            market.get("end_date"),
                            market.get("endDate"),
                            market.get("endDateIso"),
                            event.get("end_date"),
                            event.get("endDate"),
                            event.get("endDateIso"),
                            default="",
                        )
                    ),
                    "fetched_at": fetched_at,
                    "data_source_status": status,
                }
            )
    return normalized


def fetch_live_markets(limit: int = 50, timeout: float = 8.0) -> FetchResult:
    attempted_at = utc_now_iso()
    url = gamma_events_url(limit)
    headers = {
        "Accept": "application/json",
        "User-Agent": "DemoPredictionMarketViewer/0.2 (+local-preview; no-trading)",
    }
    http_status: int | None = None
    try:
        response = httpx.get(url, headers=headers, timeout=timeout)
        http_status = response.status_code
        if response.status_code != 200:
            error = f"Gamma API returned HTTP {response.status_code}"
            _write_text(GAMMA_ERROR_PATH, error)
            result = FetchResult(False, "live_failed", error, 0, 0, [], attempted_at, url, http_status)
            write_status_file(result, live_enabled=True)
            return result
        try:
            raw_json = response.json()
        except json.JSONDecodeError as exc:
            error = f"Gamma API JSON parse failed: {exc}"
            _write_text(GAMMA_ERROR_PATH, error)
            result = FetchResult(False, "live_failed", error, 0, 0, [], attempted_at, url, http_status)
            write_status_file(result, live_enabled=True)
            return result

        _write_json(GAMMA_RESPONSE_PATH, raw_json)
        raw_items = _extract_items(raw_json)
        try:
            markets = normalize_events(raw_json, source="polymarket", status="live", fetched_at=attempted_at)[:limit]
        except Exception as exc:
            error = f"Gamma API normalization failed: {exc}"
            _write_text(GAMMA_ERROR_PATH, error)
            result = FetchResult(
                False, "live_failed", error, len(raw_items), 0, [], attempted_at, url, http_status
            )
            write_status_file(result, live_enabled=True)
            return result

        status = "live" if markets else "live_empty"
        result = FetchResult(
            bool(markets),
            status,
            None if markets else "Gamma API returned no displayable markets",
            len(raw_items),
            len(markets),
            markets,
            attempted_at,
            url,
            http_status,
        )
        write_status_file(result, live_enabled=True)
        return result
    except httpx.HTTPError as exc:
        error = f"Gamma API request failed: {exc}"
    except Exception as exc:
        error = f"Gamma API fetch failed: {exc}"

    _write_text(GAMMA_ERROR_PATH, error)
    result = FetchResult(False, "live_failed", error, 0, 0, [], attempted_at, url, http_status)
    write_status_file(result, live_enabled=True)
    return result


def sample_fetch_result(
    *,
    limit: int = 50,
    status: str = "sample_fallback",
    error: str | None = None,
    live_enabled: bool = False,
) -> FetchResult:
    attempted_at = utc_now_iso()
    events = load_sample_events()
    markets = normalize_events(events, source="sample", status=status, fetched_at=attempted_at)[:limit]
    result = FetchResult(
        ok=bool(markets),
        status=status,
        error=error,
        raw_count=len(events),
        normalized_count=len(markets),
        markets=markets,
        attempted_at=attempted_at,
        url=str(SAMPLE_PATH),
        http_status=None,
        fallback_used=status != "sample_fallback",
    )
    write_status_file(result, live_enabled=live_enabled)
    return result


def load_markets(*, live: bool = False, limit: int = 50) -> list[dict[str, Any]]:
    if live:
        result = fetch_live_markets(limit=limit)
        if result.ok:
            return result.markets
        fallback_status = "live_empty_sample_fallback" if result.status == "live_empty" else "live_failed_sample_fallback"
        return sample_fetch_result(limit=limit, status=fallback_status, error=result.error).markets
    return sample_fetch_result(limit=limit).markets
