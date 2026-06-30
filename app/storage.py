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
            balance_after real not null,
            entry_type text not null,
            note text not null,
            created_at text not null default current_timestamp
        );
        create table if not exists simulated_orders (
            id integer primary key autoincrement,
            user_id text not null,
            market_id text not null,
            outcome text not null,
            stake real not null,
            probability real not null,
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
            created_at text not null default current_timestamp
        );
        """
    )
    existing = conn.execute("select user_id from demo_users where user_id = ?", (DEMO_USER_ID,)).fetchone()
    if existing is None:
        conn.execute("insert into demo_users(user_id, balance) values (?, ?)", (DEMO_USER_ID, INITIAL_DEMO_POINTS))
        conn.execute(
            "insert into demo_point_ledger(user_id, amount, balance_after, entry_type, note) values (?, ?, ?, ?, ?)",
            (DEMO_USER_ID, INITIAL_DEMO_POINTS, INITIAL_DEMO_POINTS, "initial", "initial demo points"),
        )
    conn.commit()


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


def get_balance(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> float:
    row = conn.execute("select balance from demo_users where user_id = ?", (user_id,)).fetchone()
    if row is None:
        raise ValueError("demo user missing")
    return float(row["balance"])


def list_positions(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from simulated_positions where user_id = ? order by id desc",
        (user_id,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_orders(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    rows = conn.execute("select * from simulated_orders where user_id = ? order by id desc", (user_id,)).fetchall()
    return [dict(row) for row in rows]


def list_ledger(conn: sqlite3.Connection, user_id: str = DEMO_USER_ID) -> list[dict[str, Any]]:
    rows = conn.execute("select * from demo_point_ledger where user_id = ? order by id desc", (user_id,)).fetchall()
    return [dict(row) for row in rows]
