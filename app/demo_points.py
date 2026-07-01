from __future__ import annotations

import sqlite3
from typing import Any

from app.market_display import classify_market_for_display
from app.storage import DEMO_USER_ID, get_balance, get_market


class DemoPredictionError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def estimated_simulation_return(stake: float, probability: float) -> float:
    if probability <= 0:
        return 0.0
    return round(stake / probability, 2)


def create_demo_prediction(
    conn: sqlite3.Connection,
    *,
    market_id: str,
    outcome: str,
    stake: Any,
    user_id: str = DEMO_USER_ID,
) -> dict[str, Any]:
    market = get_market(conn, market_id)
    if market is None:
        raise DemoPredictionError("market not found", 404)
    classification = classify_market_for_display(market)
    if not classification.is_demo_participation_allowed:
        reason = classification.reasons[0] if classification.reasons else "デモ参加対象外"
        raise DemoPredictionError(f"demo participation not allowed: {reason}")
    try:
        numeric_stake = float(stake)
    except (TypeError, ValueError):
        raise DemoPredictionError("stake must be numeric")
    if numeric_stake <= 0:
        raise DemoPredictionError("stake must be greater than 0")
    outcomes = market["outcomes"]
    if outcome not in outcomes:
        raise DemoPredictionError("invalid outcome")
    balance = get_balance(conn, user_id)
    if numeric_stake > balance:
        raise DemoPredictionError("insufficient demo points")

    probability = float(market["probabilities"].get(outcome, 0))
    estimated_return = estimated_simulation_return(numeric_stake, probability)
    updated_balance = round(balance - numeric_stake, 2)

    with conn:
        conn.execute("update demo_users set balance = ? where user_id = ?", (updated_balance, user_id))
        order_cursor = conn.execute(
            "insert into simulated_orders(user_id, market_id, outcome, stake, probability) values (?, ?, ?, ?, ?)",
            (user_id, market_id, outcome, numeric_stake, probability),
        )
        position_cursor = conn.execute(
            "insert into simulated_positions(user_id, market_id, outcome, stake, probability, estimated_return) values (?, ?, ?, ?, ?, ?)",
            (user_id, market_id, outcome, numeric_stake, probability, estimated_return),
        )
        conn.execute(
            "insert into demo_point_ledger(user_id, market_id, amount, balance_after, entry_type, note) values (?, ?, ?, ?, ?, ?)",
            (user_id, market_id, -numeric_stake, updated_balance, "prediction", f"demo prediction: {outcome}"),
        )

    position = {
        "id": position_cursor.lastrowid,
        "user_id": user_id,
        "market_id": market_id,
        "outcome": outcome,
        "stake": numeric_stake,
        "probability": probability,
        "estimated_return": estimated_return,
        "order_id": order_cursor.lastrowid,
    }
    return {"balance": updated_balance, "position": position}
