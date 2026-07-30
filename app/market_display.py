from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any


PUBLIC_TITLE_MAX_LENGTH = 160
PUBLIC_QUESTION_MAX_LENGTH = 220


def _truncate_public_text(value: Any, maximum: int) -> str:
    text = str(value or "").strip()
    return text if len(text) <= maximum else f"{text[:maximum - 1].rstrip()}…"


def public_market_view(market: dict[str, Any], *, summary: str) -> dict[str, Any]:
    """Return the allowlisted market shape used by public HTML and APIs.

    Stored payloads intentionally remain untouched for audit, settlement, and
    translation workflows. Descriptions, rules, source URLs, slugs, and source
    imagery are deliberately excluded from this public projection.
    """
    title = _truncate_public_text(market.get("display_title") or market.get("title"), PUBLIC_TITLE_MAX_LENGTH)
    question = _truncate_public_text(market.get("display_question") or market.get("question"), PUBLIC_QUESTION_MAX_LENGTH)
    projected = {
        "market_id": market.get("market_id"),
        "title": title,
        "question": question,
        "display_title": title,
        "display_question": question,
        "public_summary": summary,
        "outcomes": market.get("outcomes") or [],
        "probabilities": market.get("probabilities") or {},
        "volume": market.get("volume"),
        "volume_24hr": market.get("volume_24hr"),
        "liquidity": market.get("liquidity"),
        "best_bid": market.get("best_bid"),
        "best_ask": market.get("best_ask"),
        "last_trade_price": market.get("last_trade_price"),
        "realtime_spread": market.get("realtime_spread"),
        "end_date": market.get("end_date"),
        "active": bool(market.get("active")),
        "closed": bool(market.get("closed")),
        "resolved_outcome": market.get("winning_outcome") or market.get("resolved_outcome"),
        "fetched_at": market.get("fetched_at"),
        "ws_last_event_at": market.get("ws_last_event_at"),
        "data_source_status": market.get("data_source_status"),
        "display_status": market.get("display_status"),
        "display_reasons": market.get("display_reasons") or [],
        "demo_participation_allowed": bool(market.get("demo_participation_allowed")),
        "demo_participation_block_reason": market.get("demo_participation_block_reason") or "",
    }
    return projected


@dataclass(frozen=True)
class MarketDisplayClassification:
    is_displayable: bool
    is_demo_participation_allowed: bool
    reasons: list[str]
    status_label: str


def _parse_datetime(value: Any) -> datetime | None:
    if value in (None, ""):
        return None
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    text = str(value).strip()
    if not text:
        return None
    try:
        if text.endswith("Z"):
            text = f"{text[:-1]}+00:00"
        parsed = datetime.fromisoformat(text)
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _float(value: Any, default: float = 0.0) -> float:
    try:
        if value in (None, ""):
            return default
        return float(value)
    except (TypeError, ValueError):
        return default


def _probabilities(market: dict[str, Any]) -> list[float]:
    values = market.get("probabilities")
    if not isinstance(values, dict):
        return []
    probabilities: list[float] = []
    for outcome in market.get("outcomes") or []:
        if outcome in values:
            probabilities.append(_float(values[outcome]))
    return probabilities


def classify_market_for_display(
    market: dict[str, Any],
    now: datetime | None = None,
) -> MarketDisplayClassification:
    now = now or datetime.now(timezone.utc)
    if now.tzinfo is None:
        now = now.replace(tzinfo=timezone.utc)

    reasons: list[str] = []
    active = bool(market.get("active", False))
    closed = bool(market.get("closed", False))
    outcomes = market.get("outcomes")
    probabilities = _probabilities(market)
    end_date = _parse_datetime(market.get("end_date"))
    liquidity = _float(market.get("liquidity"), 0.0)

    if not active:
        reasons.append("非アクティブ")
    if closed:
        reasons.append("終了済み")
    if end_date and end_date <= now:
        reasons.append("期限切れ")
    if not isinstance(outcomes, list) or not outcomes:
        reasons.append("アウトカムなし")
    if not probabilities or len(probabilities) < len(outcomes or []):
        reasons.append("確率なし")
    if probabilities and all(probability <= 0.0 or probability >= 1.0 for probability in probabilities):
        reasons.append("確率が確定済みに近い")
    if liquidity <= 0.0:
        reasons.append("流動性なし")

    is_displayable = not reasons
    has_open_probability = any(0.0 < probability < 1.0 for probability in probabilities)
    is_demo_participation_allowed = (
        is_displayable
        and active
        and not closed
        and not (end_date and end_date <= now)
        and has_open_probability
        and liquidity > 0.0
    )

    if is_demo_participation_allowed:
        status_label = "デモ参加可"
    elif is_displayable:
        status_label = "表示のみ"
    else:
        status_label = reasons[0] if reasons else "デモ参加対象外"

    return MarketDisplayClassification(
        is_displayable=is_displayable,
        is_demo_participation_allowed=is_demo_participation_allowed,
        reasons=reasons,
        status_label=status_label,
    )


def enrich_market_for_display(market: dict[str, Any], now: datetime | None = None) -> dict[str, Any]:
    classification = classify_market_for_display(market, now)
    block_reason = "" if classification.is_demo_participation_allowed else (
        classification.reasons[0] if classification.reasons else "デモ参加対象外"
    )
    enriched = dict(market)
    enriched.update(
        {
            "display_status": classification.status_label,
            "display_reasons": classification.reasons,
            "demo_participation_allowed": classification.is_demo_participation_allowed,
            "demo_participation_block_reason": block_reason,
        }
    )
    return enriched


def market_filter_counts(markets: list[dict[str, Any]], now: datetime | None = None) -> dict[str, int]:
    counts = {
        "total_market_count": len(markets),
        "displayable_market_count": 0,
        "hidden_closed_count": 0,
        "hidden_inactive_count": 0,
        "hidden_expired_count": 0,
        "hidden_no_liquidity_count": 0,
        "hidden_resolved_probability_count": 0,
    }
    for market in markets:
        classification = classify_market_for_display(market, now)
        if classification.is_displayable:
            counts["displayable_market_count"] += 1
        reasons = set(classification.reasons)
        if "終了済み" in reasons:
            counts["hidden_closed_count"] += 1
        if "非アクティブ" in reasons:
            counts["hidden_inactive_count"] += 1
        if "期限切れ" in reasons:
            counts["hidden_expired_count"] += 1
        if "流動性なし" in reasons:
            counts["hidden_no_liquidity_count"] += 1
        if "確率が確定済みに近い" in reasons:
            counts["hidden_resolved_probability_count"] += 1
    return counts


def _sort_key(market: dict[str, Any]) -> tuple[int, float, float, float]:
    classification = classify_market_for_display(market)
    end_date = _parse_datetime(market.get("end_date"))
    end_timestamp = end_date.timestamp() if end_date else 0.0
    return (
        1 if classification.is_displayable else 0,
        _float(market.get("volume_24hr")),
        _float(market.get("liquidity")),
        end_timestamp,
    )


def filtered_market_response(
    markets: list[dict[str, Any]],
    *,
    include_closed: bool = False,
    include_expired: bool = False,
    include_inactive: bool = False,
    include_all: bool = False,
    now: datetime | None = None,
) -> dict[str, Any]:
    counts = market_filter_counts(markets, now)
    filters_applied = {
        "include_closed": include_closed,
        "include_expired": include_expired,
        "include_inactive": include_inactive,
        "include_all": include_all,
    }

    selected: list[dict[str, Any]] = []
    for market in markets:
        classification = classify_market_for_display(market, now)
        reasons = set(classification.reasons)
        include = classification.is_displayable or include_all
        include = include or (include_closed and "終了済み" in reasons)
        include = include or (include_expired and "期限切れ" in reasons)
        include = include or (include_inactive and "非アクティブ" in reasons)
        if include:
            selected.append(enrich_market_for_display(market, now))

    selected.sort(key=_sort_key, reverse=True)
    return {
        "markets": selected,
        "count": len(selected),
        **counts,
        "filters_applied": filters_applied,
        "latest_filter_run_at": datetime.now(timezone.utc).isoformat(),
    }
