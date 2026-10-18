from __future__ import annotations

from contextlib import contextmanager
from decimal import Decimal, InvalidOperation
import sqlite3
from typing import Any
from uuid import uuid4

from app.market_display import classify_market_for_display
from app.storage import (
    DEMO_USER_ID,
    create_pending_settlement_for_position,
    get_balance,
    get_market,
    get_position_by_idempotency_key,
    get_settlement_by_position_id,
    insert_audit_event,
    insert_ledger_entry,
)


class DemoPredictionError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _normalize_stake(value: Any) -> tuple[Decimal, float]:
    """Return the V1 REAL-compatible canonical stake after finite Decimal validation."""
    try:
        decimal_stake = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise DemoPredictionError("stake must be numeric") from None
    if not decimal_stake.is_finite():
        raise DemoPredictionError("stake must be finite")
    if decimal_stake <= 0:
        raise DemoPredictionError("stake must be greater than 0")
    numeric_stake = float(decimal_stake)
    if not Decimal(str(numeric_stake)).is_finite():
        raise DemoPredictionError("stake must be finite")
    # V1 stores REAL values. Canonicalize through that persisted representation
    # so equivalent text/number inputs compare compatibly with existing rows.
    return Decimal(str(numeric_stake)), numeric_stake


def _matches_idempotency_payload(
    position: dict[str, Any],
    *,
    market_id: str,
    outcome: str,
    stake: Decimal,
) -> bool:
    if position["market_id"] != market_id or position["outcome"] != outcome:
        return False
    try:
        existing_stake, _ = _normalize_stake(position["stake"])
    except DemoPredictionError:
        return False
    return existing_stake == stake


@contextmanager
def _prediction_transaction(conn: sqlite3.Connection):
    """Serialize top-level V1 balance writes and preserve caller transactions."""
    if conn.in_transaction:
        savepoint = f"demo_prediction_{id(conn)}"
        conn.execute(f"savepoint {savepoint}")
        try:
            yield
        except Exception:
            conn.execute(f"rollback to savepoint {savepoint}")
            conn.execute(f"release savepoint {savepoint}")
            raise
        else:
            conn.execute(f"release savepoint {savepoint}")
        return

    conn.execute("begin immediate")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


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
    idempotency_key: str | None = None,
    request_id: str | None = None,
    max_stake: float | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    normalized_stake, numeric_stake = _normalize_stake(stake)

    with _prediction_transaction(conn):
        if idempotency_key:
            existing_position = get_position_by_idempotency_key(
                conn,
                user_id=user_id,
                idempotency_key=idempotency_key,
            )
            if existing_position:
                if not _matches_idempotency_payload(
                    dict(existing_position),
                    market_id=market_id,
                    outcome=outcome,
                    stake=normalized_stake,
                ):
                    raise DemoPredictionError("idempotency key payload conflict", 409)
                settlement = get_settlement_by_position_id(conn, int(existing_position["id"]))
                position = dict(existing_position)
                position["settlement_status"] = settlement["status"] if settlement else "pending"
                insert_audit_event(
                    conn,
                    event_type="demo_prediction_replayed",
                    user_id=user_id,
                    route="/api/demo/predict",
                    request_id=request_id,
                    reference_type="simulated_position",
                    reference_id=position["id"],
                    after={"position_id": position["id"], "idempotency_key": idempotency_key},
                    note="idempotent demo participation replay",
                )
                return {"balance": get_balance(conn, user_id), "position": position, "idempotent_replay": True}

        market = get_market(conn, market_id)
        if market is None:
            raise DemoPredictionError("market not found", 404)
        classification = classify_market_for_display(market)
        if not classification.is_demo_participation_allowed:
            reason = classification.reasons[0] if classification.reasons else "デモ参加対象外"
            raise DemoPredictionError(f"demo participation not allowed: {reason}")
        if max_stake is not None and normalized_stake > Decimal(str(max_stake)):
            raise DemoPredictionError("stake is above the allowed demo point limit")
        outcomes = market["outcomes"]
        if outcome not in outcomes:
            raise DemoPredictionError("invalid outcome")

        probability = float(market["probabilities"].get(outcome, 0))
        estimated_return = estimated_simulation_return(numeric_stake, probability)
        balance = get_balance(conn, user_id)
        if numeric_stake > balance:
            raise DemoPredictionError("insufficient demo points")
        updated_balance = round(balance - numeric_stake, 2)
        updated = conn.execute(
            "update demo_users set balance = ? where user_id = ? and balance >= ?",
            (updated_balance, user_id, numeric_stake),
        )
        if updated.rowcount != 1:
            raise DemoPredictionError("insufficient demo points")
        order_cursor = conn.execute(
            """
            insert into simulated_orders(user_id, market_id, outcome, stake, probability, idempotency_key, request_id)
            values (?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, market_id, outcome, numeric_stake, probability, idempotency_key, request_id),
        )
        position_cursor = conn.execute(
            """
            insert into simulated_positions(
                user_id, market_id, outcome, stake, probability, estimated_return, idempotency_key, request_id
            ) values (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (user_id, market_id, outcome, numeric_stake, probability, estimated_return, idempotency_key, request_id),
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
            "idempotency_key": idempotency_key,
            "request_id": request_id,
        }
        insert_ledger_entry(
            conn,
            user_id=user_id,
            market_id=market_id,
            amount=-numeric_stake,
            balance_before=balance,
            balance_after=updated_balance,
            entry_type="prediction",
            note=f"demo prediction: {outcome}",
            reference_type="simulated_position",
            reference_id=position_cursor.lastrowid,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        settlement = create_pending_settlement_for_position(conn, position)
        insert_audit_event(
            conn,
            event_type="demo_prediction_created",
            user_id=user_id,
            route="/api/demo/predict",
            request_id=request_id,
            reference_type="simulated_position",
            reference_id=position_cursor.lastrowid,
            before={"balance": balance},
            after={"balance": updated_balance, "position_id": position_cursor.lastrowid},
            note="デモポジション作成",
        )
    position["settlement_status"] = settlement["status"]
    return {"balance": updated_balance, "position": position, "idempotent_replay": False}
