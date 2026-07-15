"""Deterministic, secret-free evidence used exclusively to settle demo markets."""
from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
from typing import Any


def _first(*values: Any) -> Any:
    return next((value for value in values if value not in (None, "")), None)


def _json_list(value: Any) -> list[Any] | None:
    if isinstance(value, str):
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return None
    return value if isinstance(value, list) else None


def _bool(value: Any) -> bool:
    return value is True or (isinstance(value, str) and value.strip().lower() in {"true", "1", "yes"})


def canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def normalize_rest_evidence(requested_market_id: str, payload: Any, *, fetched_at: str | None = None, source: str = "polymarket_gamma_market_detail") -> dict[str, Any]:
    """Keep only resolution facts.  Never persist headers, credentials, or raw payloads."""
    fetched_at = fetched_at or datetime.now(timezone.utc).isoformat()
    if not isinstance(payload, dict):
        return {"requested_market_id": requested_market_id, "source_type": source, "fetched_at": fetched_at, "malformed": True}
    outcomes = _json_list(payload.get("outcomes"))
    token_ids = _json_list(_first(payload.get("clobTokenIds"), payload.get("clob_token_ids"), payload.get("tokenIds"), payload.get("outcomeTokenIds")))
    prices = _json_list(_first(payload.get("outcomePrices"), payload.get("outcome_prices"), payload.get("lastPrices")))
    return {
        "requested_market_id": str(requested_market_id),
        "external_market_id": str(_first(payload.get("id"), payload.get("market_id"), payload.get("external_market_id")) or ""),
        "condition_id": _first(payload.get("conditionId"), payload.get("condition_id")),
        "closed": _bool(payload.get("closed")),
        "resolved": _bool(_first(payload.get("resolved"), payload.get("marketResolved"), payload.get("market_resolved"))),
        "outcomes": outcomes,
        "token_ids": token_ids,
        "outcome_prices": prices,
        "winning_outcome": _first(payload.get("winningOutcome"), payload.get("winning_outcome"), payload.get("winningOutcomeName")),
        "resolved_outcome": _first(payload.get("resolvedOutcome"), payload.get("resolved_outcome"), payload.get("resolution_outcome")),
        "winning_asset_id": _first(payload.get("winningAssetId"), payload.get("winning_asset_id")),
        "upstream_updated_at": _first(payload.get("updatedAt"), payload.get("updated_at"), payload.get("endDateIso")),
        "fetched_at": fetched_at,
        "source_type": source,
    }


def evidence_hash(evidence: dict[str, Any]) -> str:
    return hashlib.sha256(canonical_json(evidence).encode("utf-8")).hexdigest()


def validate_settlement_evidence(evidence: dict[str, Any]) -> dict[str, Any]:
    codes: list[str] = []
    if evidence.get("malformed"):
        return {"status": "invalid", "winning_outcome": None, "winning_token_id": None, "failure_codes": ["malformed_response"], "details": {}}
    if evidence.get("requested_market_id") != evidence.get("external_market_id"):
        codes.append("market_id_mismatch")
    outcomes, tokens = evidence.get("outcomes"), evidence.get("token_ids")
    if not evidence.get("external_market_id"):
        codes.append("missing_market_id")
    if not isinstance(outcomes, list) or not outcomes or any(not isinstance(x, str) or not x.strip() for x in outcomes):
        codes.append("missing_or_invalid_outcomes")
    if not isinstance(tokens, list) or not tokens or any(not isinstance(x, str) or not x.strip() for x in tokens):
        codes.append("missing_or_invalid_token_ids")
    if isinstance(outcomes, list) and isinstance(tokens, list):
        if len(outcomes) != len(tokens): codes.append("outcome_token_length_mismatch")
        if len(set(outcomes)) != len(outcomes): codes.append("duplicate_outcome")
        if len(set(tokens)) != len(tokens): codes.append("duplicate_token_id")
    if codes:
        return {"status": "invalid", "winning_outcome": None, "winning_token_id": None, "failure_codes": codes, "details": {}}
    if not (evidence.get("closed") or evidence.get("resolved")):
        return {"status": "unresolved", "winning_outcome": None, "winning_token_id": None, "failure_codes": ["market_not_closed_or_resolved"], "details": {}}
    by_token = dict(zip(tokens, outcomes))
    candidates = [value for value in (evidence.get("winning_outcome"), evidence.get("resolved_outcome")) if value]
    asset = evidence.get("winning_asset_id")
    if asset:
        if asset not in by_token:
            return {"status": "invalid", "winning_outcome": None, "winning_token_id": None, "failure_codes": ["winning_asset_not_in_tokens"], "details": {}}
        candidates.append(by_token[asset])
    if not candidates:
        return {"status": "unresolved", "winning_outcome": None, "winning_token_id": None, "failure_codes": ["missing_explicit_winner"], "details": {}}
    if any(candidate not in outcomes for candidate in candidates):
        return {"status": "invalid", "winning_outcome": None, "winning_token_id": None, "failure_codes": ["winner_not_in_outcomes"], "details": {}}
    if len(set(candidates)) != 1:
        return {"status": "conflict", "winning_outcome": None, "winning_token_id": None, "failure_codes": ["winner_fields_conflict"], "details": {"candidates": candidates}}
    winner = candidates[0]
    return {"status": "confirmed", "winning_outcome": winner, "winning_token_id": tokens[outcomes.index(winner)], "failure_codes": [], "details": {}}
