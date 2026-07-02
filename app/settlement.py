from __future__ import annotations

from datetime import datetime, timezone
import sqlite3
from typing import Any

from app.storage import (
    DEMO_USER_ID,
    get_balance,
    get_demo_settlement,
    get_market,
    list_pending_settlements,
    settlement_ledger_entry_exists,
    update_demo_settlement,
)

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


def classify_settlement(settlement: dict[str, Any], market: dict[str, Any]) -> dict[str, Any]:
    winning_outcome = extract_winning_outcome(market)
    if winning_outcome is None:
        return {
            "status": "settlement_pending",
            "winning_outcome": None,
            "payout": 0.0,
            "settlement_source": "public_market_data",
            "settlement_note": "公開マーケットデータから明確な結果を確認できないため、判定保留です。",
        }

    if str(settlement["outcome"]) == str(winning_outcome):
        return {
            "status": "settled_win",
            "winning_outcome": winning_outcome,
            "payout": float(settlement["estimated_return"]),
            "settlement_source": "public_market_data",
            "settlement_note": "明確な結果に基づき、ローカルのデモ精算を記録しました。",
        }
    return {
        "status": "settled_loss",
        "winning_outcome": winning_outcome,
        "payout": 0.0,
        "settlement_source": "public_market_data",
        "settlement_note": "明確な結果に基づき、ローカルのデモ精算を記録しました。",
    }


def settle_one(conn: sqlite3.Connection, settlement_id: int) -> dict[str, Any]:
    settlement = get_demo_settlement(conn, settlement_id)
    if settlement is None:
        raise ValueError("demo settlement missing")
    if settlement["status"] in SETTLED_STATUSES:
        return settlement

    market = get_market(conn, settlement["market_id"])
    if market is None:
        with conn:
            return update_demo_settlement(
                conn,
                settlement_id,
                status="settlement_unknown",
                winning_outcome=None,
                payout=0.0,
                settlement_source="local_storage",
                settlement_note="保存済みマーケットが見つからないため、判定不明です。",
                settled_at=None,
            )

    classified = classify_settlement(settlement, market)
    status = classified["status"]
    settled_at = datetime.now(timezone.utc).isoformat() if status in SETTLED_STATUSES else None
    marker = f"settlement_id={settlement_id}"

    with conn:
        current = get_demo_settlement(conn, settlement_id)
        if current is None:
            raise ValueError("demo settlement missing")
        if current["status"] in SETTLED_STATUSES:
            return current

        if status == "settled_win":
            payout = float(classified["payout"])
            if not settlement_ledger_entry_exists(conn, settlement_id):
                balance_after = round(get_balance(conn, current["user_id"]) + payout, 2)
                conn.execute("update demo_users set balance = ? where user_id = ?", (balance_after, current["user_id"]))
                conn.execute(
                    """
                    insert into demo_point_ledger(user_id, market_id, amount, balance_after, entry_type, note)
                    values (?, ?, ?, ?, ?, ?)
                    """,
                    (
                        current["user_id"],
                        current["market_id"],
                        payout,
                        balance_after,
                        "settlement_win",
                        f"demo settlement win: {marker}",
                    ),
                )
        elif status == "settled_loss" and not settlement_ledger_entry_exists(conn, settlement_id):
            conn.execute(
                """
                insert into demo_point_ledger(user_id, market_id, amount, balance_after, entry_type, note)
                values (?, ?, ?, ?, ?, ?)
                """,
                (
                    current["user_id"],
                    current["market_id"],
                    0.0,
                    get_balance(conn, current["user_id"]),
                    "settlement_loss",
                    f"demo settlement loss: {marker}",
                ),
            )

        return update_demo_settlement(
            conn,
            settlement_id,
            status=status,
            winning_outcome=classified["winning_outcome"],
            payout=float(classified["payout"]),
            settlement_source=classified["settlement_source"],
            settlement_note=classified["settlement_note"],
            settled_at=settled_at,
        )


def settle_pending_positions(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
    pending = list_pending_settlements(conn, user_id)
    checked_count = len(pending)
    settled_win_count = 0
    settled_loss_count = 0
    pending_count = 0
    unknown_count = 0
    total_payout = 0.0

    for settlement in pending:
        result = settle_one(conn, int(settlement["id"]))
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
    }
