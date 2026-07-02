from __future__ import annotations

import json
from pathlib import Path
import sqlite3
from typing import Any

DEMO_USER_ID = "local-demo-user"
INITIAL_DEMO_POINTS = 10000.0


def connect(db_path: str) -> sqlite3.Connection:
    if db_path != ":memory:":
        Path(db_path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    conn.executescript(
        """
        create table if not exists markets (
            market_id text primary key,
            payload text not null,
            updated_at text not null
        );
        create table if not exists market_snapshots (
            id integer primary key autoincrement,
            market_id text not null,
            payload text not null,
            fetched_at text not null
        );
        create table if not exists market_realtime_updates (
            id integer primary key autoincrement,
            market_id text,
            asset_id text,
            event_type text not null,
            best_bid real,
            best_ask real,
            last_trade_price real,
            price real,
            size real,
            side text,
            spread real,
            winning_outcome text,
            winning_asset_id text,
            raw_event_json text not null,
            event_timestamp text,
            received_at text not null default current_timestamp
        );
        create table if not exists fetch_runs (
            id integer primary key autoincrement,
            fetched_at text not null,
            status text not null,
            market_count integer not null
        );
        create table if not exists demo_users (
            user_id text primary key,
            balance real not null
        );
        create table if not exists demo_point_ledger (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text,
            amount real not null,
            balance_before real,
            balance_after real not null,
            entry_type text not null,
            note text not null,
            reference_type text,
            reference_id text,
            idempotency_key text,
            request_id text,
            created_at text not null default current_timestamp
        );
        create table if not exists demo_audit_events (
            id integer primary key autoincrement,
            event_type text not null,
            user_id text,
            route text,
            request_id text,
            reference_type text,
            reference_id text,
            before_json text,
            after_json text,
            note text,
            created_at text not null default current_timestamp
        );
        create table if not exists simulated_orders (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text not null,
            outcome text not null,
            stake real not null,
            probability real not null,
            idempotency_key text,
            request_id text,
            created_at text not null default current_timestamp
        );
        create table if not exists simulated_positions (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text not null,
            outcome text not null,
            stake real not null,
            probability real not null,
            estimated_return real not null,
            idempotency_key text,
            request_id text,
            created_at text not null default current_timestamp
        );
        create table if not exists demo_settlements (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text not null,
            position_id integer not null,
            outcome text not null,
            stake real not null,
            probability real not null,
            estimated_return real not null,
            status text not null,
            winning_outcome text,
            payout real not null default 0,
            settlement_source text,
            settlement_note text,
            settled_at text,
            created_at text not null default current_timestamp
        );
        """
    )
    _ensure_column(conn, "demo_point_ledger", "balance_before", "real")
    _ensure_column(conn, "demo_point_ledger", "reference_type", "text")
    _ensure_column(conn, "demo_point_ledger", "reference_id", "text")
    _ensure_column(conn, "demo_point_ledger", "idempotency_key", "text")
    _ensure_column(conn, "demo_point_ledger", "request_id", "text")
    _ensure_column(conn, "simulated_orders", "idempotency_key", "text")
    _ensure_column(conn, "simulated_orders", "request_id", "text")
    _ensure_column(conn, "simulated_positions", "idempotency_key", "text")
    _ensure_column(conn, "simulated_positions", "request_id", "text")
    existing = conn.execute("select user_id from demo_users where user_id = ?", (DEMO_USER_ID,)).fetchone()
    if existing is None:
        conn.execute("insert into demo_users(user_id, balance) values (?, ?)", (DEMO_USER_ID, INITIAL_DEMO_POINTS))
        conn.execute(
            "insert into demo_point_ledger(user_id, amount, balance_after, entry_type, note) values (?, ?, ?, ?, ?)",
            (DEMO_USER_ID, INITIAL_DEMO_POINTS, INITIAL_DEMO_POINTS, "initial", "initial demo points"),
        )
    conn.commit()


def _ensure_column(conn: sqlite3.Connection, table: str, column: str, declaration: str) -> None:
    columns = {row["name"] for row in conn.execute(f"pragma table_info({table})").fetchall()}
    if column not in columns:
        conn.execute(f"alter table {table} add column {column} {declaration}")


def store_markets(conn: sqlite3.Connection, markets: list[dict[str, Any]]) -> None:
    for market in markets:
        payload = json.dumps(market, ensure_ascii=False)
        conn.execute(
            "insert into markets(market_id, payload, updated_at) values (?, ?, ?) "
            "on conflict(market_id) do update set payload = excluded.payload, updated_at = excluded.updated_at",
            (market["market_id"], payload, market["fetched_at"]),
        )
        conn.execute(
            "insert into market_snapshots(market_id, payload, fetched_at) values (?, ?, ?)",
            (market["market_id"], payload, market["fetched_at"]),
        )
    status = markets[0]["data_source_status"] if markets else "empty"
    fetched_at = markets[0]["fetched_at"] if markets else ""
    conn.execute(
        "insert into fetch_runs(fetched_at, status, market_count) values (?, ?, ?)",
        (fetched_at, status, len(markets)),
    )
    conn.commit()


def replace_markets(conn: sqlite3.Connection, markets: list[dict[str, Any]]) -> None:
    conn.execute("delete from markets")
    conn.commit()
    store_markets(conn, markets)


def list_markets(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("select payload from markets order by json_extract(payload, '$.volume_24hr') desc").fetchall()
    return [json.loads(row["payload"]) for row in rows]


def get_last_fetch_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("select * from fetch_runs order by id desc limit 1").fetchone()
    return dict(row) if row else None


def get_market(conn: sqlite3.Connection, market_id: str) -> dict[str, Any] | None:
    row = conn.execute("select payload from markets where market_id = ?", (market_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def list_snapshots(conn: sqlite3.Connection, market_id: str, limit: int = 20) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select payload, fetched_at from market_snapshots where market_id = ? order by id desc limit ?",
        (market_id, limit),
    ).fetchall()
    return [{"fetched_at": row["fetched_at"], "market": json.loads(row["payload"])} for row in rows]


def insert_realtime_update(conn: sqlite3.Connection, update: dict[str, Any]) -> dict[str, Any]:
    cursor = conn.execute(
        """
        insert into market_realtime_updates(
            market_id, asset_id, event_type, best_bid, best_ask, last_trade_price, price,
            size, side, spread, winning_outcome, winning_asset_id, raw_event_json, event_timestamp
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            update.get("market_id"),
            update.get("asset_id"),
            update["event_type"],
            update.get("best_bid"),
            update.get("best_ask"),
            update.get("last_trade_price"),
            update.get("price"),
            update.get("size"),
            update.get("side"),
            update.get("spread"),
            update.get("winning_outcome"),
            update.get("winning_asset_id"),
            update["raw_event_json"],
            update.get("event_timestamp"),
        ),
    )
    row = conn.execute("select * from market_realtime_updates where id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def get_latest_realtime_update(conn: sqlite3.Connection, market_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select * from market_realtime_updates
        where market_id = ?
        order by datetime(coalesce(event_timestamp, received_at)) desc, id desc
        limit 1
        """,
        (market_id,),
    ).fetchone()
    return dict(row) if row else None


def list_latest_realtime_updates(conn: sqlite3.Connection, market_ids: list[str]) -> dict[str, dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for market_id in market_ids:
        update = get_latest_realtime_update(conn, market_id)
        if update:
            latest[market_id] = update
    return latest


def latest_realtime_status(conn: sqlite3.Connection) -> dict[str, Any]:
    row = conn.execute(
        """
        select max(coalesce(event_timestamp, received_at)) as latest_update_at,
               count(*) as update_count,
               count(distinct market_id) as market_update_count
        from market_realtime_updates
        """
    ).fetchone()
    return {
        "latest_update_at": row["latest_update_at"] if row else None,
        "update_count": int(row["update_count"] or 0) if row else 0,
        "market_update_count": int(row["market_update_count"] or 0) if row else 0,
    }


def list_resolution_candidate_updates(
    conn: sqlite3.Connection,
    market_id: str | None = None,
    limit: int = 100,
) -> list[dict[str, Any]]:
    sql = """
        select * from market_realtime_updates
        where event_type = 'market_resolved'
          and (winning_outcome is not null or winning_asset_id is not null)
    """
    params: list[Any] = []
    if market_id is not None:
        sql += " and market_id = ?"
        params.append(market_id)
    sql += " order by datetime(coalesce(event_timestamp, received_at)) desc, id desc limit ?"
    params.append(limit)
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def get_latest_resolution_candidate(conn: sqlite3.Connection, market_id: str) -> dict[str, Any] | None:
    candidates = list_resolution_candidate_updates(conn, market_id=market_id, limit=1)
    return candidates[0] if candidates else None


def count_resolution_candidates(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        """
        select count(*) as count from market_realtime_updates
        where event_type = 'market_resolved'
          and (winning_outcome is not null or winning_asset_id is not null)
        """
    ).fetchone()
    return int(row["count"] or 0)


def list_markets_with_resolution_candidates(conn: sqlite3.Connection) -> list[str]:
    rows = conn.execute(
        """
        select distinct market_id from market_realtime_updates
        where event_type = 'market_resolved'
          and market_id is not null
          and (winning_outcome is not null or winning_asset_id is not null)
        order by market_id
        """
    ).fetchall()
    return [str(row["market_id"]) for row in rows]


def get_balance(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> float:
    row = conn.execute("select balance from demo_users where user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("demo user missing")
    return float(row["balance"])


def insert_ledger_entry(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    amount: float,
    balance_after: float,
    entry_type: str,
    note: str,
    market_id: str | None = None,
    balance_before: float | None = None,
    reference_type: str | None = None,
    reference_id: str | int | None = None,
    idempotency_key: str | None = None,
    request_id: str | None = None,
) -> dict[str, Any]:
    cursor = conn.execute(
        """
        insert into demo_point_ledger(
            user_id, market_id, amount, balance_before, balance_after, entry_type, note,
            reference_type, reference_id, idempotency_key, request_id
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            user_id,
            market_id,
            float(amount),
            balance_before,
            float(balance_after),
            entry_type,
            note,
            reference_type,
            str(reference_id) if reference_id is not None else None,
            idempotency_key,
            request_id,
        ),
    )
    row = conn.execute("select * from demo_point_ledger where id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def find_ledger_by_idempotency_key(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    entry_type: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select * from demo_point_ledger
        where user_id = ? and entry_type = ? and idempotency_key = ?
        order by id desc limit 1
        """,
        (user_id, entry_type, idempotency_key),
    ).fetchone()
    return dict(row) if row else None


def insert_audit_event(
    conn: sqlite3.Connection,
    *,
    event_type: str,
    user_id: str | None = None,
    route: str | None = None,
    request_id: str | None = None,
    reference_type: str | None = None,
    reference_id: str | int | None = None,
    before: Any = None,
    after: Any = None,
    note: str | None = None,
) -> dict[str, Any]:
    cursor = conn.execute(
        """
        insert into demo_audit_events(
            event_type, user_id, route, request_id, reference_type, reference_id,
            before_json, after_json, note
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            user_id,
            route,
            request_id,
            reference_type,
            str(reference_id) if reference_id is not None else None,
            json.dumps(before, ensure_ascii=False) if before is not None else None,
            json.dumps(after, ensure_ascii=False) if after is not None else None,
            note,
        ),
    )
    row = conn.execute("select * from demo_audit_events where id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def list_audit_events(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from demo_audit_events order by id desc limit ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def ledger_summary(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> dict[str, Any]:
    rows = list_ledger(conn, user_id)
    return {
        "total_added": round(sum(float(row["amount"]) for row in rows if row["entry_type"] == "demo_point_add"), 2),
        "total_used_for_demo_participation": round(
            abs(sum(float(row["amount"]) for row in rows if row["entry_type"] == "prediction")),
            2,
        ),
        "total_settled": round(
            sum(float(row["amount"]) for row in rows if row["entry_type"] in {"settlement_win", "settlement_loss"}),
            2,
        ),
        "total_adjusted": round(sum(float(row["amount"]) for row in rows if row["entry_type"] == "demo_balance_reset"), 2),
        "ledger_count": len(rows),
    }


def list_positions(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from simulated_positions where user_id = ? order by id desc",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def get_position_by_id(conn: sqlite3.Connection, position_id: int) -> dict[str, Any] | None:
    row = conn.execute("select * from simulated_positions where id = ?", (position_id,)).fetchone()
    return dict(row) if row else None


def get_position_by_idempotency_key(
    conn: sqlite3.Connection,
    *,
    user_id: str,
    idempotency_key: str,
) -> dict[str, Any] | None:
    row = conn.execute(
        """
        select * from simulated_positions
        where user_id = ? and idempotency_key = ?
        order by id desc limit 1
        """,
        (user_id, idempotency_key),
    ).fetchone()
    return dict(row) if row else None


def get_settlement_by_position_id(conn: sqlite3.Connection, position_id: int) -> dict[str, Any] | None:
    row = conn.execute("select * from demo_settlements where position_id = ? order by id desc limit 1", (position_id,)).fetchone()
    return dict(row) if row else None


def get_demo_settlement(conn: sqlite3.Connection, settlement_id: int) -> dict[str, Any] | None:
    row = conn.execute("select * from demo_settlements where id = ?", (settlement_id,)).fetchone()
    return dict(row) if row else None


def create_pending_settlement_for_position(conn: sqlite3.Connection, position: dict[str, Any]) -> dict[str, Any]:
    existing = get_settlement_by_position_id(conn, int(position["id"]))
    if existing:
        return existing
    cursor = conn.execute(
        """
        insert into demo_settlements(
            user_id, market_id, position_id, outcome, stake, probability, estimated_return,
            status, winning_outcome, payout, settlement_source, settlement_note, settled_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            position["user_id"],
            position["market_id"],
            int(position["id"]),
            position["outcome"],
            float(position["stake"]),
            float(position["probability"]),
            float(position["estimated_return"]),
            "pending",
            None,
            0.0,
            "local_demo",
            "結果待ち。自動精算はまだ実装していません。",
            None,
        ),
    )
    row = conn.execute("select * from demo_settlements where id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def ensure_pending_settlements(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> None:
    for position in list_positions(conn, user_id):
        create_pending_settlement_for_position(conn, position)
    conn.commit()


def list_demo_results(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    ensure_pending_settlements(conn, user_id)
    rows = conn.execute(
        "select * from demo_settlements where user_id = ? order by id desc",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_pending_settlements(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    ensure_pending_settlements(conn, user_id)
    rows = conn.execute(
        """
        select * from demo_settlements
        where user_id = ? and status in ('pending', 'settlement_pending', 'settlement_unknown')
        order by id asc
        """,
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def update_demo_settlement(
    conn: sqlite3.Connection,
    settlement_id: int,
    *,
    status: str,
    winning_outcome: str | None,
    payout: float,
    settlement_source: str | None,
    settlement_note: str | None,
    settled_at: str | None,
) -> dict[str, Any]:
    conn.execute(
        """
        update demo_settlements
        set status = ?,
            winning_outcome = ?,
            payout = ?,
            settlement_source = ?,
            settlement_note = ?,
            settled_at = ?
        where id = ?
        """,
        (status, winning_outcome, float(payout), settlement_source, settlement_note, settled_at, settlement_id),
    )
    updated = get_demo_settlement(conn, settlement_id)
    if updated is None:
        raise ValueError("demo settlement missing")
    return updated


def settlement_ledger_entry_exists(conn: sqlite3.Connection, settlement_id: int) -> bool:
    marker = f"settlement_id={settlement_id}"
    row = conn.execute(
        "select id from demo_point_ledger where note like ? limit 1",
        (f"%{marker}%",),
    ).fetchone()
    return row is not None


def list_orders(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    rows = conn.execute("select * from simulated_orders where user_id = ? order by id desc", (user_id,)).fetchall()
    return [dict(row) for row in rows]


def list_ledger(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    rows = conn.execute("select * from demo_point_ledger where user_id = ? order by id desc", (user_id,)).fetchall()
    return [dict(row) for row in rows]
