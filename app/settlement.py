from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any

from app.storage import (
    DEMO_USER_ID,
    get_balance,
    get_demo_settlement,
    get_latest_resolution_candidate,
    get_market,
    insert_audit_event,
    insert_ledger_entry,
    insert_settlement_evidence,
    list_pending_settlements,
    settlement_ledger_entry_exists,
    update_demo_settlement,
)
from app.polymarket_gamma import fetch_market_detail_for_settlement
from app.settlement_evidence import evidence_hash, normalize_rest_evidence, validate_settlement_evidence

PENDING_STATUSES = {"pending", "settlement_pending", "settlement_unknown"}
SETTLED_STATUSES = {"settled_win", "settled_loss"}


def _first(*values: Any) -> Any:
    for value in values:
        if value not in (None, ""):
            return value
    return None


def _bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on", "resolved", "closed"}
    return bool(value)


def _outcome_lookup(market: dict[str, Any]) -> dict[str, str]:
    outcomes = market.get("outcomes") or []
    lookup: dict[str, str] = {}
    token_keys = (
        "clobTokenIds",
        "clob_token_ids",
        "clobTokenIDs",
        "token_ids",
        "tokenIds",
        "outcomeTokenIds",
    )
    token_ids = _first(*(market.get(key) for key in token_keys))
    if isinstance(token_ids, str):
        token_ids = [part.strip() for part in token_ids.strip("[]").replace('"', "").split(",") if part.strip()]
    if isinstance(token_ids, list) and len(token_ids) == len(outcomes):
        for token_id, outcome in zip(token_ids, outcomes):
            lookup[str(token_id)] = str(outcome)

    tokens = market.get("tokens")
    if isinstance(tokens, list):
        for token in tokens:
            if not isinstance(token, dict):
                continue
            token_id = _first(token.get("id"), token.get("token_id"), token.get("tokenId"), token.get("asset_id"))
            outcome = _first(token.get("outcome"), token.get("name"), token.get("label"))
            if token_id and outcome:
                lookup[str(token_id)] = str(outcome)
    return lookup


def extract_winning_outcome(market: dict[str, Any]) -> str | None:
    explicit = _first(
        market.get("winning_outcome"),
        market.get("winningOutcome"),
        market.get("resolved_outcome"),
        market.get("resolution_outcome"),
        market.get("winningOutcomeName"),
    )
    if explicit:
        return str(explicit)

    winning_asset_id = _first(market.get("winning_asset_id"), market.get("winningAssetId"))
    if winning_asset_id:
        return _outcome_lookup(market).get(str(winning_asset_id))

    is_closed_or_resolved = any(
        _bool(market.get(key))
        for key in ("closed", "resolved", "market_resolved", "marketResolved")
    )
    outcomes = market.get("outcomes")
    probabilities = market.get("probabilities")
    if not is_closed_or_resolved or not isinstance(outcomes, list) or not isinstance(probabilities, dict):
        return None

    values: list[tuple[str, float]] = []
    for outcome in outcomes:
        if outcome not in probabilities:
            return None
        try:
            values.append((str(outcome), float(probabilities[outcome])))
        except (TypeError, ValueError):
            return None

    winners = [outcome for outcome, probability in values if probability >= 0.999]
    others_are_zero = all(probability <= 0.001 for outcome, probability in values if outcome not in winners)
    if len(winners) == 1 and others_are_zero:
        return winners[0]
    return None


def _candidate_winning_outcome(candidate: dict[str, Any] | None, market: dict[str, Any]) -> str | None:
    if not candidate:
        return None
    if candidate.get("winning_outcome"):
        return str(candidate["winning_outcome"])
    if candidate.get("winning_asset_id"):
        return _outcome_lookup(market).get(str(candidate["winning_asset_id"]))
    return None


def get_resolution_candidate_for_market(conn: sqlite3.Connection, market_id: str) -> dict[str, Any] | None:
    return get_latest_resolution_candidate(conn, market_id)


def compare_candidate_with_rest_resolution(candidate: dict[str, Any] | None, market: dict[str, Any]) -> dict[str, Any]:
    candidate_outcome = _candidate_winning_outcome(candidate, market)
    candidate_asset_id = str(candidate["winning_asset_id"]) if candidate and candidate.get("winning_asset_id") else None
    rest_outcome = extract_winning_outcome(market)

    if rest_outcome and candidate and candidate_outcome == rest_outcome:
        return {
            "candidate_winning_outcome": candidate_outcome,
            "candidate_winning_asset_id": candidate_asset_id,
            "rest_winning_outcome": rest_outcome,
            "confirmation_status": "confirmed_match",
            "settlement_source": "ws_candidate_rest_confirmed",
            "note": "WS検知の結果候補とREST確認が一致しました。",
        }
    if rest_outcome and not candidate:
        return {
            "candidate_winning_outcome": None,
            "candidate_winning_asset_id": None,
            "rest_winning_outcome": rest_outcome,
            "confirmation_status": "rest_clear_without_candidate",
            "settlement_source": "rest_conservative",
            "note": "REST判定で明確な結果を確認しました。",
        }
    if candidate and not rest_outcome:
        return {
            "candidate_winning_outcome": candidate_outcome,
            "candidate_winning_asset_id": candidate_asset_id,
            "rest_winning_outcome": None,
            "confirmation_status": "candidate_only_unconfirmed",
            "settlement_source": "ws_candidate_unconfirmed",
            "note": "WS検知の結果候補がありますが、REST確認が未完了です。",
        }
    if rest_outcome and candidate and candidate_outcome != rest_outcome:
        return {
            "candidate_winning_outcome": candidate_outcome,
            "candidate_winning_asset_id": candidate_asset_id,
            "rest_winning_outcome": rest_outcome,
            "confirmation_status": "conflict",
            "settlement_source": "ws_candidate_conflict",
            "note": "WS/REST不一致のため、ローカルのデモ精算を保留します。",
        }
    return {
        "candidate_winning_outcome": candidate_outcome,
        "candidate_winning_asset_id": candidate_asset_id,
        "rest_winning_outcome": rest_outcome,
        "confirmation_status": "unclear",
        "settlement_source": "unresolved",
        "note": "WS検知とREST確認のどちらからも明確な結果を確認できません。",
    }


def classify_settlement(settlement: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    confirmation = compare_candidate_with_rest_resolution(None, market)
    winning_outcome = confirmation["rest_winning_outcome"]
    if confirmation["confirmation_status"] not in {"confirmed_match", "rest_clear_without_candidate"} or winning_outcome is None:
        return {
            "status": "settlement_pending",
            "winning_outcome": None,
            "payout": 0.0,
            "settlement_source": confirmation["settlement_source"],
            "settlement_note": confirmation["note"],
            "confirmation": confirmation,
        }

    if str(settlement["outcome"]) == str(winning_outcome):
        return {
            "status": "settled_win",
            "winning_outcome": winning_outcome,
            "payout": float(settlement["estimated_return"]),
            "settlement_source": confirmation["settlement_source"],
            "settlement_note": "REST確認済みの結果に基づき、ローカルのデモ精算を記録しました。",
            "confirmation": confirmation,
        }
    return {
        "status": "settled_loss",
        "winning_outcome": winning_outcome,
        "payout": 0.0,
        "settlement_source": confirmation["settlement_source"],
        "settlement_note": "REST確認済みの結果に基づき、ローカルのデモ精算を記録しました。",
        "confirmation": confirmation,
    }


def _classify_settlement_with_candidate(
    conn: sqlite3.Connection,
    settlement: dict[str, Any],
    market: dict[str, Any],
) -> dict[str, Any]:
    candidate = get_resolution_candidate_for_market(conn, settlement["market_id"])
    confirmation = compare_candidate_with_rest_resolution(candidate, market)
    status = confirmation["confirmation_status"]
    winning_outcome = confirmation["rest_winning_outcome"]
    if status in {"candidate_only_unconfirmed", "unclear"}:
        return {
            "status": "settlement_pending",
            "winning_outcome": None,
            "payout": 0.0,
            "settlement_source": confirmation["settlement_source"],
            "settlement_note": confirmation["note"],
            "confirmation": confirmation,
        }
    if status == "conflict":
        return {
            "status": "settlement_unknown",
            "winning_outcome": None,
            "payout": 0.0,
            "settlement_source": confirmation["settlement_source"],
            "settlement_note": confirmation["note"],
            "confirmation": confirmation,
        }
    if str(settlement["outcome"]) == str(winning_outcome):
        return {
            "status": "settled_win",
            "winning_outcome": winning_outcome,
            "payout": float(settlement["estimated_return"]),
            "settlement_source": confirmation["settlement_source"],
            "settlement_note": "REST確認済みの結果に基づき、ローカルのデモ精算を記録しました。",
            "confirmation": confirmation,
        }
    return {
        "status": "settled_loss",
        "winning_outcome": winning_outcome,
        "payout": 0.0,
        "settlement_source": confirmation["settlement_source"],
        "settlement_note": "REST確認済みの結果に基づき、ローカルのデモ精算を記録しました。",
        "confirmation": confirmation,
    }


def settle_one(conn: sqlite3.Connection, settlement_id: int, *, market_detail_fetcher=None) -> dict[str, Any]:
    """Settle only after an independent, just-in-time REST confirmation.

    The network request deliberately occurs before ``BEGIN IMMEDIATE``; all evidence,
    financial writes and audit entries then share the same SQLite transaction.
    """
    settlement = get_demo_settlement(conn, settlement_id)
    if settlement is None:
        raise ValueError("demo settlement missing")
    if settlement["status"] in SETTLED_STATUSES:
        return settlement
    detail = (market_detail_fetcher or fetch_market_detail_for_settlement)(str(settlement["market_id"]))
    if detail.ok and detail.payload is not None:
        evidence = normalize_rest_evidence(str(settlement["market_id"]), detail.payload, fetched_at=detail.fetched_at)
        validation = validate_settlement_evidence(evidence)
    else:
        evidence = {
            "requested_market_id": str(settlement["market_id"]), "external_market_id": str(settlement["market_id"]),
            "source_type": "polymarket_gamma_market_detail", "fetched_at": detail.fetched_at,
            "upstream_updated_at": None, "fetch_status": detail.status,
        }
        validation = {"status": "unavailable", "winning_outcome": None, "winning_token_id": None,
                      "failure_codes": ["upstream_unavailable"], "details": {}}
    current_hash = evidence_hash(evidence)

    # BEGIN IMMEDIATE serializes competing workers after their short REST calls.
    conn.execute("BEGIN IMMEDIATE")
    try:
        current = get_demo_settlement(conn, settlement_id)
        if current is None:
            raise ValueError("demo settlement missing")
        if current["status"] in SETTLED_STATUSES:
            conn.rollback()
            return current
        insert_settlement_evidence(conn, market_id=current["market_id"], settlement_id=settlement_id,
                                   evidence=evidence, evidence_hash=current_hash, validation=validation)
        if validation["status"] != "confirmed":
            mapped_status = "settlement_unknown" if validation["status"] in {"conflict", "invalid"} else "settlement_pending"
            updated = update_demo_settlement(conn, settlement_id, status=mapped_status, winning_outcome=None, payout=0.0,
                                             settlement_source="fresh_rest_evidence", settlement_note=validation["status"], settled_at=None)
            insert_audit_event(conn, event_type="settlement_evidence_not_confirmed", user_id=current["user_id"],
                               route="/api/demo/settle", reference_type="demo_settlement", reference_id=settlement_id,
                               after={"market_id": current["market_id"], "evidence_hash": current_hash, "fetched_at": evidence["fetched_at"],
                                      "status": validation["status"], "failure_codes": validation["failure_codes"]}, note="fresh REST evidence did not confirm settlement")
            conn.commit()
            updated.update({"evidence_status": validation["status"], "failure_codes": validation["failure_codes"], "evidence_hash": current_hash})
            return updated

        winner = str(validation["winning_outcome"])
        status = "settled_win" if str(current["outcome"]) == winner else "settled_loss"
        payout = float(current["estimated_return"]) if status == "settled_win" else 0.0
        marker = f"settlement_id={settlement_id}"
        if status == "settled_win":
            if not settlement_ledger_entry_exists(conn, settlement_id):
                balance_before = get_balance(conn, current["user_id"])
                balance_after = round(balance_before + payout, 2)
                conn.execute("update demo_users set balance = ? where user_id = ?", (balance_after, current["user_id"]))
                insert_ledger_entry(
                    conn,
                    user_id=current["user_id"],
                    market_id=current["market_id"],
                    amount=payout,
                    balance_before=balance_before,
                    balance_after=balance_after,
                    entry_type="settlement_win",
                    note=f"demo settlement win: {marker}",
                    reference_type="demo_settlement",
                    reference_id=settlement_id,
                )
                insert_audit_event(
                    conn,
                    event_type="settlement_paid",
                    user_id=current["user_id"],
                    route="/api/demo/settle",
                    reference_type="demo_settlement",
                    reference_id=settlement_id,
                    before={"balance": balance_before},
                    after={"balance": balance_after, "payout": payout},
                    note="デモ精算",
                )
        elif not settlement_ledger_entry_exists(conn, settlement_id):
            balance_before = get_balance(conn, current["user_id"])
            insert_ledger_entry(
                conn,
                user_id=current["user_id"],
                market_id=current["market_id"],
                amount=0.0,
                balance_before=balance_before,
                balance_after=balance_before,
                entry_type="settlement_loss",
                note=f"demo settlement loss: {marker}",
                reference_type="demo_settlement",
                reference_id=settlement_id,
            )
            insert_audit_event(
                conn,
                event_type="settlement_loss",
                user_id=current["user_id"],
                route="/api/demo/settle",
                reference_type="demo_settlement",
                reference_id=settlement_id,
                before={"balance": balance_before},
                after={"balance": balance_before, "payout": 0.0},
                note="デモ精算",
            )

        updated = update_demo_settlement(conn, settlement_id, status=status, winning_outcome=winner, payout=payout,
                                         settlement_source="rest_conservative", settlement_note="fresh REST evidence confirmed",
                                         settled_at=datetime.now(timezone.utc).isoformat())
        insert_audit_event(
            conn,
            event_type="settlement_checked",
            user_id=current["user_id"],
            route="/api/demo/settle",
            reference_type="demo_settlement",
            reference_id=settlement_id,
            after={"market_id": current["market_id"], "status": status, "winning_outcome": winner,
                   "winning_token_id": validation["winning_token_id"], "evidence_hash": current_hash,
                   "fetched_at": evidence["fetched_at"]},
            note="fresh REST evidence confirmed settlement",
        )
        conn.commit()
        updated.update({"evidence_status": "confirmed", "evidence_hash": current_hash,
                        "winning_token_id": validation["winning_token_id"]})
        return updated
    except Exception:
        conn.rollback()
        raise


def settle_pending_positions(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
    pending = list_pending_settlements(conn, user_id)
    checked_count = len(pending)
    settled_win_count = 0
    settled_loss_count = 0
    pending_count = 0
    unknown_count = 0
    total_payout = 0.0
    ws_candidate_count = 0
    ws_confirmed_count = 0
    ws_unconfirmed_count = 0
    ws_conflict_count = 0
    rest_only_settled_count = 0

    for settlement in pending:
        result = settle_one(conn, int(settlement["id"]))
        confirmation_status = result.get("confirmation_status")
        if result.get("candidate_winning_outcome") or result.get("candidate_winning_asset_id"):
            ws_candidate_count += 1
        if confirmation_status == "confirmed_match":
            ws_confirmed_count += 1
        elif confirmation_status == "candidate_only_unconfirmed":
            ws_unconfirmed_count += 1
        elif confirmation_status == "conflict":
            ws_conflict_count += 1
        elif confirmation_status == "rest_clear_without_candidate" and result["status"] in SETTLED_STATUSES:
            rest_only_settled_count += 1
        if result["status"] == "settled_win":
            settled_win_count += 1
            total_payout += float(result["payout"])
        elif result["status"] == "settled_loss":
            settled_loss_count += 1
        elif result["status"] == "settlement_unknown":
            unknown_count += 1
        else:
            pending_count += 1

    return {
        "checked_count": checked_count,
        "settled_win_count": settled_win_count,
        "settled_loss_count": settled_loss_count,
        "pending_count": pending_count,
        "unknown_count": unknown_count,
        "total_payout": round(total_payout, 2),
        "balance": get_balance(conn, user_id),
        "ws_candidate_count": ws_candidate_count,
        "ws_confirmed_count": ws_confirmed_count,
        "ws_unconfirmed_count": ws_unconfirmed_count,
        "ws_conflict_count": ws_conflict_count,
        "rest_only_settled_count": rest_only_settled_count,
    }
