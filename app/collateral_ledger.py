"""Fully collateralized outcome-ledger primitives for the v2 engine.

This module intentionally has no HTTP, order-book, resolution, or network code.
All monetary values are integer micro-points.
"""

from __future__ import annotations

from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import sqlite3
from typing import Any, Iterator

from app.storage import get_market, insert_audit_event


ENGINE_KEY = "collateralized_clob_v2"
LEGACY_ENGINE_KEY = "fixed_odds_v1"
POINT_SCALE = 10_000
OPERATOR_TREASURY_OWNER_ID = "operator-treasury"
SQLITE_INTEGER_MAX = 9_223_372_036_854_775_807


class CollateralLedgerError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _payload_hash(value: dict[str, Any]) -> str:
    payload = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _positive_integer(value: Any, code: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise CollateralLedgerError(code)
    if value <= 0:
        raise CollateralLedgerError(code)
    return value


def _checked_micro(quantity: int) -> int:
    if quantity > SQLITE_INTEGER_MAX // POINT_SCALE:
        raise CollateralLedgerError("integer_overflow")
    return quantity * POINT_SCALE


@contextmanager
def _write_transaction(conn: sqlite3.Connection) -> Iterator[None]:
    """Serialize top-level financial writes and nest safely under caller work."""
    if conn.in_transaction:
        savepoint = f"collateral_ledger_{id(conn)}"
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

    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
    except Exception:
        conn.rollback()
        raise
    else:
        conn.commit()


def _require_engine(conn: sqlite3.Connection) -> None:
    row = conn.execute(
        "select status from prediction_engines where engine_key = ?", (ENGINE_KEY,)
    ).fetchone()
    if row is None or row["status"] != "available":
        raise CollateralLedgerError("engine_unavailable")


def _treasury_account_id() -> str:
    return f"{ENGINE_KEY}:operator:{OPERATOR_TREASURY_OWNER_ID}"


def _ensure_treasury_account(conn: sqlite3.Connection, now: str) -> sqlite3.Row:
    account_id = _treasury_account_id()
    conn.execute(
        """
        insert or ignore into point_accounts(
            account_id, engine_key, owner_type, owner_id, created_at, updated_at
        ) values (?, ?, 'operator', ?, ?, ?)
        """,
        (account_id, ENGINE_KEY, OPERATOR_TREASURY_OWNER_ID, now, now),
    )
    return conn.execute("select * from point_accounts where account_id = ?", (account_id,)).fetchone()


def _get_v2_account(conn: sqlite3.Connection, account_id: str) -> sqlite3.Row:
    row = conn.execute("select * from point_accounts where account_id = ?", (account_id,)).fetchone()
    if row is None or row["engine_key"] != ENGINE_KEY:
        raise CollateralLedgerError("account_missing")
    return row


def _get_open_market(conn: sqlite3.Connection, market_id: str) -> tuple[sqlite3.Row, sqlite3.Row]:
    market = conn.execute("select * from collateral_markets where market_id = ?", (market_id,)).fetchone()
    if market is None:
        raise CollateralLedgerError("market_missing")
    if market["engine_key"] != ENGINE_KEY or market["status"] != "open":
        raise CollateralLedgerError("market_not_open")
    reserve = conn.execute("select * from market_reserves where market_id = ?", (market_id,)).fetchone()
    if reserve is None:
        raise CollateralLedgerError("invariant_violation")
    return market, reserve


def _event_replay(
    conn: sqlite3.Connection, *, account_id: str, idempotency_key: str, payload_hash: str
) -> sqlite3.Row | None:
    event = conn.execute(
        "select * from reserve_events where engine_key = ? and account_id = ? and idempotency_key = ?",
        (ENGINE_KEY, account_id, idempotency_key),
    ).fetchone()
    if event is None:
        return None
    if event["payload_hash"] != payload_hash:
        raise CollateralLedgerError("idempotency_conflict")
    return event


def _supply_replay(conn: sqlite3.Connection, *, idempotency_key: str, payload_hash: str) -> sqlite3.Row | None:
    event = conn.execute(
        "select * from point_supply_events where engine_key = ? and idempotency_key = ?",
        (ENGINE_KEY, idempotency_key),
    ).fetchone()
    if event is None:
        return None
    if event["payload_hash"] != payload_hash:
        raise CollateralLedgerError("idempotency_conflict")
    return event


def _result_from_event(event: sqlite3.Row, *, replay: bool) -> dict[str, Any]:
    return {
        "event_id": int(event["id"]),
        "event_type": event["event_type"],
        "quantity": int(event["quantity"]),
        "points_micro": int(event["points_micro"]),
        "idempotent_replay": replay,
    }


def _record_ledger(
    conn: sqlite3.Connection,
    *,
    account_id: str | None,
    market_id: str | None,
    entry_type: str,
    amount_micro: int,
    account_before: int | None,
    account_after: int | None,
    reserve_before: int | None,
    reserve_after: int | None,
    reference_type: str,
    reference_id: str,
    request_id: str | None,
    now: str,
) -> None:
    conn.execute(
        """
        insert into collateral_ledger_entries(
            engine_key, account_id, market_id, entry_type, amount_micro,
            account_available_before_micro, account_available_after_micro,
            reserve_before_micro, reserve_after_micro, reference_type,
            reference_id, request_id, created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (ENGINE_KEY, account_id, market_id, entry_type, amount_micro, account_before, account_after,
         reserve_before, reserve_after, reference_type, reference_id, request_id, now),
    )


def _ensure_verified(conn: sqlite3.Connection, market_id: str) -> None:
    result = verify_collateral_invariants(conn, market_id=market_id)
    if result["integrity_status"] != "verified":
        raise CollateralLedgerError("invariant_violation")


def bootstrap_v2_point_supply(
    conn: sqlite3.Connection,
    *,
    amount_micro: int,
    idempotency_key: str,
    request_id: str | None = None,
) -> dict[str, Any]:
    amount = _positive_integer(amount_micro, "invalid_amount")
    if amount > SQLITE_INTEGER_MAX:
        raise CollateralLedgerError("integer_overflow")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise CollateralLedgerError("idempotency_conflict")
    payload_hash = _payload_hash({"operation": "bootstrap", "amount_micro": amount})

    with _write_transaction(conn):
        _require_engine(conn)
        replay = _supply_replay(conn, idempotency_key=idempotency_key, payload_hash=payload_hash)
        if replay is not None:
            return {
                "event_id": int(replay["id"]), "amount_micro": int(replay["amount_micro"]),
                "destination_account_id": replay["destination_account_id"], "idempotent_replay": True,
            }
        state = conn.execute("select * from point_supply_state where engine_key = ?", (ENGINE_KEY,)).fetchone()
        if state is None:
            raise CollateralLedgerError("engine_unavailable")
        if state["bootstrap_completed"]:
            raise CollateralLedgerError("bootstrap_already_completed")
        if int(state["issued_micro"]) > SQLITE_INTEGER_MAX - amount:
            raise CollateralLedgerError("integer_overflow")

        now = _now()
        treasury = _ensure_treasury_account(conn, now)
        account_before = int(treasury["available_micro"])
        if account_before > SQLITE_INTEGER_MAX - amount:
            raise CollateralLedgerError("integer_overflow")
        account_after = account_before + amount
        conn.execute(
            "update point_accounts set available_micro = ?, version = version + 1, updated_at = ? where account_id = ?",
            (account_after, now, treasury["account_id"]),
        )
        conn.execute(
            "update point_supply_state set issued_micro = issued_micro + ?, bootstrap_completed = 1, version = version + 1, updated_at = ? where engine_key = ?",
            (amount, now, ENGINE_KEY),
        )
        cursor = conn.execute(
            """
            insert into point_supply_events(
                engine_key, event_type, destination_account_id, amount_micro, idempotency_key,
                request_id, payload_hash, created_at
            ) values (?, 'bootstrap_issue', ?, ?, ?, ?, ?, ?)
            """,
            (ENGINE_KEY, treasury["account_id"], amount, idempotency_key, request_id, payload_hash, now),
        )
        reference_id = str(cursor.lastrowid)
        _record_ledger(
            conn, account_id=treasury["account_id"], market_id=None, entry_type="bootstrap_issue",
            amount_micro=amount, account_before=account_before, account_after=account_after,
            reserve_before=None, reserve_after=None, reference_type="point_supply_event",
            reference_id=reference_id, request_id=request_id, now=now,
        )
        insert_audit_event(
            conn, event_type="v2_point_supply_bootstrapped", request_id=request_id,
            reference_type="point_supply_event", reference_id=reference_id,
            after={"amount_micro": amount, "engine_key": ENGINE_KEY}, note="v2 operator treasury bootstrap",
        )
        _ensure_verified(conn, market_id="")
        return {"event_id": int(cursor.lastrowid), "amount_micro": amount,
                "destination_account_id": treasury["account_id"], "idempotent_replay": False}


def create_collateral_market(
    conn: sqlite3.Connection, *, market_id: str, status: str = "open"
) -> dict[str, Any]:
    if not isinstance(market_id, str) or not market_id.strip():
        raise CollateralLedgerError("market_missing")
    if status != "open":
        raise CollateralLedgerError("market_not_open")
    with _write_transaction(conn):
        _require_engine(conn)
        existing = conn.execute("select * from collateral_markets where market_id = ?", (market_id,)).fetchone()
        if existing is not None:
            if existing["engine_key"] != ENGINE_KEY or existing["status"] != "open":
                raise CollateralLedgerError("market_not_open")
            return {"market_id": market_id, "status": "open", "idempotent_replay": True}
        source_market = get_market(conn, market_id)
        if source_market is None:
            raise CollateralLedgerError("market_missing")
        if source_market.get("outcomes") != ["YES", "NO"]:
            raise CollateralLedgerError("market_not_open")
        now = _now()
        conn.execute(
            "insert into collateral_markets(market_id, engine_key, status, point_scale, created_at, updated_at) values (?, ?, 'open', ?, ?, ?)",
            (market_id, ENGINE_KEY, POINT_SCALE, now, now),
        )
        conn.execute("insert into market_reserves(market_id, updated_at) values (?, ?)", (market_id, now))
        insert_audit_event(
            conn, event_type="v2_collateral_market_created", reference_type="collateral_market",
            reference_id=market_id, after={"engine_key": ENGINE_KEY, "status": "open"},
            note="v2 collateral market created",
        )
        _ensure_verified(conn, market_id=market_id)
        return {"market_id": market_id, "status": "open", "idempotent_replay": False}


def _complete_set_operation(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    account_id: str,
    market_id: str,
    quantity: int,
    idempotency_key: str,
    request_id: str | None,
) -> dict[str, Any]:
    quantity = _positive_integer(quantity, "invalid_quantity")
    points_micro = _checked_micro(quantity)
    if not isinstance(account_id, str) or not account_id or not isinstance(market_id, str) or not market_id:
        raise CollateralLedgerError("account_missing")
    if not isinstance(idempotency_key, str) or not idempotency_key.strip():
        raise CollateralLedgerError("idempotency_conflict")
    payload_hash = _payload_hash({
        "operation": event_type, "account_id": account_id, "market_id": market_id, "quantity": quantity,
    })
    with _write_transaction(conn):
        _require_engine(conn)
        replay = _event_replay(conn, account_id=account_id, idempotency_key=idempotency_key, payload_hash=payload_hash)
        if replay is not None:
            return _result_from_event(replay, replay=True)
        account = _get_v2_account(conn, account_id)
        _, reserve = _get_open_market(conn, market_id)
        now = _now()
        reserve_before = int(reserve["reserve_micro"])
        account_before = int(account["available_micro"])
        yes = conn.execute(
            "select * from outcome_positions where account_id = ? and market_id = ? and outcome = 'YES'",
            (account_id, market_id),
        ).fetchone()
        no = conn.execute(
            "select * from outcome_positions where account_id = ? and market_id = ? and outcome = 'NO'",
            (account_id, market_id),
        ).fetchone()

        if event_type == "split":
            if account_before < points_micro:
                raise CollateralLedgerError("insufficient_points")
            if reserve_before > SQLITE_INTEGER_MAX - points_micro:
                raise CollateralLedgerError("integer_overflow")
            account_after, reserve_after = account_before - points_micro, reserve_before + points_micro
            yes_available = int(yes["available_shares"]) if yes else 0
            no_available = int(no["available_shares"]) if no else 0
            if yes_available > SQLITE_INTEGER_MAX - quantity or no_available > SQLITE_INTEGER_MAX - quantity:
                raise CollateralLedgerError("integer_overflow")
            yes_after, no_after = yes_available + quantity, no_available + quantity
            ledger_specs = (("split_account_debit", -points_micro, account_before, account_after, None, None),
                            ("split_reserve_credit", points_micro, None, None, reserve_before, reserve_after))
            audit_type = "v2_complete_sets_split"
        else:
            yes_available = int(yes["available_shares"]) if yes else 0
            no_available = int(no["available_shares"]) if no else 0
            if yes_available < quantity and yes is not None and int(yes["locked_shares"]) > 0:
                raise CollateralLedgerError("locked_shares_present")
            if no_available < quantity and no is not None and int(no["locked_shares"]) > 0:
                raise CollateralLedgerError("locked_shares_present")
            if yes_available < quantity:
                raise CollateralLedgerError("insufficient_yes_shares")
            if no_available < quantity:
                raise CollateralLedgerError("insufficient_no_shares")
            if reserve_before < points_micro:
                raise CollateralLedgerError("invariant_violation")
            if account_before > SQLITE_INTEGER_MAX - points_micro:
                raise CollateralLedgerError("integer_overflow")
            account_after, reserve_after = account_before + points_micro, reserve_before - points_micro
            yes_after, no_after = yes_available - quantity, no_available - quantity
            ledger_specs = (("merge_reserve_debit", -points_micro, None, None, reserve_before, reserve_after),
                            ("merge_account_credit", points_micro, account_before, account_after, None, None))
            audit_type = "v2_complete_sets_merged"

        conn.execute(
            "update point_accounts set available_micro = ?, version = version + 1, updated_at = ? where account_id = ?",
            (account_after, now, account_id),
        )
        conn.execute(
            "update market_reserves set reserve_micro = ?, net_complete_sets = net_complete_sets + ?, version = version + 1, updated_at = ? where market_id = ?",
            (reserve_after, quantity if event_type == "split" else -quantity, now, market_id),
        )
        for outcome, current, updated in (("YES", yes, yes_after), ("NO", no, no_after)):
            if current is None:
                conn.execute(
                    "insert into outcome_positions(account_id, market_id, outcome, available_shares, updated_at) values (?, ?, ?, ?, ?)",
                    (account_id, market_id, outcome, updated, now),
                )
            else:
                conn.execute(
                    "update outcome_positions set available_shares = ?, version = version + 1, updated_at = ? where account_id = ? and market_id = ? and outcome = ?",
                    (updated, now, account_id, market_id, outcome),
                )
        cursor = conn.execute(
            """
            insert into reserve_events(engine_key, market_id, account_id, event_type, quantity, points_micro,
                reserve_before_micro, reserve_after_micro, idempotency_key, request_id, payload_hash, created_at)
            values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (ENGINE_KEY, market_id, account_id, event_type, quantity, points_micro, reserve_before,
             reserve_after, idempotency_key, request_id, payload_hash, now),
        )
        reference_id = str(cursor.lastrowid)
        for entry_type, amount, before_account, after_account, before_reserve, after_reserve in ledger_specs:
            _record_ledger(
                conn, account_id=account_id, market_id=market_id, entry_type=entry_type,
                amount_micro=amount, account_before=before_account, account_after=after_account,
                reserve_before=before_reserve, reserve_after=after_reserve,
                reference_type="reserve_event", reference_id=reference_id, request_id=request_id, now=now,
            )
        insert_audit_event(
            conn, event_type=audit_type, request_id=request_id, reference_type="reserve_event",
            reference_id=reference_id, after={"market_id": market_id, "quantity": quantity},
            note="v2 complete-set operation",
        )
        _ensure_verified(conn, market_id=market_id)
        return {"event_id": int(cursor.lastrowid), "event_type": event_type, "quantity": quantity,
                "points_micro": points_micro, "idempotent_replay": False}


def split_complete_sets(conn: sqlite3.Connection, *, account_id: str, market_id: str, quantity: int,
                        idempotency_key: str, request_id: str | None = None) -> dict[str, Any]:
    return _complete_set_operation(
        conn, event_type="split", account_id=account_id, market_id=market_id, quantity=quantity,
        idempotency_key=idempotency_key, request_id=request_id,
    )


def merge_complete_sets(conn: sqlite3.Connection, *, account_id: str, market_id: str, quantity: int,
                        idempotency_key: str, request_id: str | None = None) -> dict[str, Any]:
    return _complete_set_operation(
        conn, event_type="merge", account_id=account_id, market_id=market_id, quantity=quantity,
        idempotency_key=idempotency_key, request_id=request_id,
    )


def verify_collateral_invariants(conn: sqlite3.Connection, *, market_id: str | None = None) -> dict[str, Any]:
    """Read-only v2 accounting verification with no participant identifiers in output."""
    state = conn.execute("select issued_micro, burned_micro from point_supply_state where engine_key = ?", (ENGINE_KEY,)).fetchone()
    if state is None:
        return {"integrity_status": "failed", "engine_key": ENGINE_KEY, "market_count": 0,
                "issued_micro": 0, "burned_micro": 0, "account_micro": 0, "reserve_micro": 0,
                "violation_count": 1, "violation_codes": ["engine_unavailable"]}
    account_micro = int(conn.execute(
        "select coalesce(sum(available_micro + locked_micro), 0) from point_accounts where engine_key = ?", (ENGINE_KEY,)
    ).fetchone()[0])
    reserve_micro = int(conn.execute(
        "select coalesce(sum(r.reserve_micro), 0) from market_reserves r join collateral_markets m on m.market_id = r.market_id where m.engine_key = ?", (ENGINE_KEY,)
    ).fetchone()[0])
    markets_sql, params = "where m.engine_key = ?", [ENGINE_KEY]
    if market_id:
        markets_sql += " and m.market_id = ?"
        params.append(market_id)
    markets = conn.execute(
        f"select m.market_id, r.reserve_micro, r.net_complete_sets from collateral_markets m join market_reserves r on r.market_id = m.market_id {markets_sql}", params
    ).fetchall()
    codes: list[str] = []
    issued, burned = int(state["issued_micro"]), int(state["burned_micro"])
    if issued - burned != account_micro + reserve_micro:
        codes.append("global_point_conservation_failed")
    for row in markets:
        sets = int(row["net_complete_sets"])
        if int(row["reserve_micro"]) != sets * POINT_SCALE:
            codes.append("reserve_complete_set_mismatch")
        for outcome, code in (("YES", "yes_supply_mismatch"), ("NO", "no_supply_mismatch")):
            supply = int(conn.execute(
                "select coalesce(sum(p.available_shares + p.locked_shares), 0) from outcome_positions p where p.market_id = ? and p.outcome = ?",
                (row["market_id"], outcome),
            ).fetchone()[0])
            if supply != sets:
                codes.append(code)
    negative = conn.execute(
        "select 1 from point_accounts where engine_key = ? and (available_micro < 0 or locked_micro < 0) union all "
        "select 1 from market_reserves where reserve_micro < 0 or net_complete_sets < 0 union all "
        "select 1 from outcome_positions where available_shares < 0 or locked_shares < 0 limit 1", (ENGINE_KEY,)
    ).fetchone()
    if negative is not None:
        codes.append("negative_balance_detected")
    unique_codes = list(dict.fromkeys(codes))
    return {
        "integrity_status": "verified" if not unique_codes else "failed",
        "engine_key": ENGINE_KEY, "market_count": len(markets), "issued_micro": issued,
        "burned_micro": burned, "account_micro": account_micro, "reserve_micro": reserve_micro,
        "violation_count": len(unique_codes), "violation_codes": unique_codes,
    }
