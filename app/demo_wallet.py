from __future__ import annotations

import sqlite3
from typing import Any
from uuid import uuid4

from app.storage import (
    DEMO_USER_ID,
    INITIAL_DEMO_POINTS,
    find_ledger_by_idempotency_key,
    get_balance,
    get_ledger_entry,
    insert_audit_event,
    insert_ledger_entry,
    ledger_summary,
    list_audit_events,
    list_ledger,
)


class DemoWalletError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def _amount(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise DemoWalletError("amount must be numeric")
    if amount < 1 or amount > 100000:
        raise DemoWalletError("amount must be between 1 and 100000")
    return round(amount, 2)


def wallet_snapshot(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
    return {
        "user_id": user_id,
        "balance": get_balance(conn, user_id),
        "ledger": list_ledger(conn, user_id),
        "audit_events": list_audit_events(conn, limit=100),
        "summary": ledger_summary(conn, user_id),
    }


def add_demo_points(
    conn: sqlite3.Connection,
    *,
    amount: Any,
    reason: str | None = None,
    idempotency_key: str | None = None,
    user_id: str = DEMO_USER_ID,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    if idempotency_key:
        existing = find_ledger_by_idempotency_key(
            conn,
            user_id=user_id,
            entry_type="demo_point_add",
            idempotency_key=idempotency_key,
        )
        if existing:
            insert_audit_event(
                conn,
                event_type="demo_point_add_replayed",
                user_id=user_id,
                route="/api/demo/wallet/add-points",
                request_id=request_id,
                reference_type="demo_point_ledger",
                reference_id=existing["id"],
                after={"ledger_id": existing["id"], "idempotency_key": idempotency_key},
                note="idempotent demo point add replay",
            )
            conn.commit()
            return {"balance": get_balance(conn, user_id), "ledger_entry": existing, "idempotent_replay": True}

    numeric_amount = _amount(amount)
    note = (reason or "デモポイント追加").strip()[:200]
    balance_before = get_balance(conn, user_id)
    balance_after = round(balance_before + numeric_amount, 2)
    with conn:
        conn.execute("update demo_users set balance = ? where user_id = ?", (balance_after, user_id))
        ledger_entry = insert_ledger_entry(
            conn,
            user_id=user_id,
            amount=numeric_amount,
            balance_before=balance_before,
            balance_after=balance_after,
            entry_type="demo_point_add",
            note=note,
            reference_type="demo_point_wallet",
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        insert_audit_event(
            conn,
            event_type="demo_point_add_created",
            user_id=user_id,
            route="/api/demo/wallet/add-points",
            request_id=request_id,
            reference_type="demo_point_ledger",
            reference_id=ledger_entry["id"],
            before={"balance": balance_before},
            after={"balance": balance_after, "amount": numeric_amount},
            note=note,
        )
    return {"balance": balance_after, "ledger_entry": ledger_entry, "idempotent_replay": False}


def reset_demo_balance(
    conn: sqlite3.Connection,
    *,
    reason: str | None = None,
    idempotency_key: str | None = None,
    user_id: str = DEMO_USER_ID,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    if idempotency_key:
        existing = find_ledger_by_idempotency_key(
            conn,
            user_id=user_id,
            entry_type="demo_balance_reset",
            idempotency_key=idempotency_key,
        )
        if existing:
            insert_audit_event(
                conn,
                event_type="demo_balance_reset_replayed",
                user_id=user_id,
                route="/api/demo/wallet/reset",
                request_id=request_id,
                reference_type="demo_point_ledger",
                reference_id=existing["id"],
                after={"ledger_id": existing["id"], "idempotency_key": idempotency_key},
                note="idempotent demo balance reset replay",
            )
            conn.commit()
            return {"balance": get_balance(conn, user_id), "ledger_entry": existing, "idempotent_replay": True}

    note = (reason or "デモ残高リセット").strip()[:200]
    balance_before = get_balance(conn, user_id)
    balance_after = INITIAL_DEMO_POINTS
    amount = round(balance_after - balance_before, 2)
    with conn:
        conn.execute("update demo_users set balance = ? where user_id = ?", (balance_after, user_id))
        ledger_entry = insert_ledger_entry(
            conn,
            user_id=user_id,
            amount=amount,
            balance_before=balance_before,
            balance_after=balance_after,
            entry_type="demo_balance_reset",
            note=note,
            reference_type="demo_point_wallet",
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        insert_audit_event(
            conn,
            event_type="demo_balance_reset_created",
            user_id=user_id,
            route="/api/demo/wallet/reset",
            request_id=request_id,
            reference_type="demo_point_ledger",
            reference_id=ledger_entry["id"],
            before={"balance": balance_before},
            after={"balance": balance_after, "amount": amount},
            note=note,
        )
    return {"balance": balance_after, "ledger_entry": ledger_entry, "idempotent_replay": False}


def reverse_demo_ledger_entry(
    conn: sqlite3.Connection,
    *,
    ledger_entry_id: int,
    reason: str,
    idempotency_key: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    request_id = request_id or str(uuid4())
    original = get_ledger_entry(conn, ledger_entry_id)
    if original is None:
        raise DemoWalletError("demo ledger entry was not found", status_code=404)
    if original["entry_type"] == "correction_reversal":
        raise DemoWalletError("correction reversal entries cannot be reversed", status_code=400)

    user_id = str(original["user_id"])
    if idempotency_key:
        existing = find_ledger_by_idempotency_key(
            conn,
            user_id=user_id,
            entry_type="correction_reversal",
            idempotency_key=idempotency_key,
        )
        if existing:
            if str(existing["reference_id"]) != str(ledger_entry_id):
                raise DemoWalletError("idempotency key was already used for another correction reversal", status_code=409)
            insert_audit_event(
                conn,
                event_type="correction_reversal_replayed",
                user_id=user_id,
                route="/api/demo/ledger/reversal",
                request_id=request_id,
                reference_type="demo_point_ledger",
                reference_id=existing["id"],
                after={
                    "ledger_id": existing["id"],
                    "original_ledger_entry_id": ledger_entry_id,
                    "idempotency_key": idempotency_key,
                },
                note="idempotent local demo correction replay",
            )
            conn.commit()
            return {
                "balance": get_balance(conn, user_id),
                "ledger_entry": existing,
                "original_ledger_entry": original,
                "idempotent_replay": True,
            }

    duplicate = conn.execute(
        """
        select * from demo_point_ledger
        where entry_type = 'correction_reversal'
          and reference_type = 'demo_point_ledger'
          and reference_id = ?
        order by id desc limit 1
        """,
        (str(ledger_entry_id),),
    ).fetchone()
    if duplicate:
        raise DemoWalletError("demo ledger entry already has a correction reversal", status_code=409)

    note = (reason or "").strip()
    if not note:
        raise DemoWalletError("reason is required")
    note = f"Local demo correction reversal: {note[:160]}"
    reversal_amount = round(-float(original["amount"]), 2)
    balance_before = get_balance(conn, user_id)
    balance_after = round(balance_before + reversal_amount, 2)
    with conn:
        conn.execute("update demo_users set balance = ? where user_id = ?", (balance_after, user_id))
        ledger_entry = insert_ledger_entry(
            conn,
            user_id=user_id,
            market_id=original["market_id"],
            amount=reversal_amount,
            balance_before=balance_before,
            balance_after=balance_after,
            entry_type="correction_reversal",
            note=note,
            reference_type="demo_point_ledger",
            reference_id=ledger_entry_id,
            idempotency_key=idempotency_key,
            request_id=request_id,
        )
        insert_audit_event(
            conn,
            event_type="correction_reversal_created",
            user_id=user_id,
            route="/api/demo/ledger/reversal",
            request_id=request_id,
            reference_type="demo_point_ledger",
            reference_id=ledger_entry["id"],
            before={
                "balance": balance_before,
                "original_ledger_entry": {
                    "id": original["id"],
                    "amount": original["amount"],
                    "entry_type": original["entry_type"],
                },
            },
            after={
                "balance": balance_after,
                "amount": reversal_amount,
                "original_ledger_entry_id": ledger_entry_id,
            },
            note=note,
        )
    return {
        "balance": balance_after,
        "ledger_entry": ledger_entry,
        "original_ledger_entry": original,
        "idempotent_replay": False,
    }
