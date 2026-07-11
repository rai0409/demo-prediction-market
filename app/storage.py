from __future__ import annotations

from datetime import datetime, timezone
import hashlib
import json
import math
from pathlib import Path
import re
import sqlite3
from typing import Any

DEMO_USER_ID = "participant-1"
INITIAL_DEMO_POINTS = 10000.0
USER_ID_PATTERN = re.compile(r"[^a-zA-Z0-9_.-]+")


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
        create table if not exists market_translations (
            market_id text not null,
            language text not null,
            translated_title text,
            translated_question text,
            translated_description text,
            source_title_hash text not null,
            source_question_hash text not null,
            source_description_hash text not null,
            translation_provider text not null,
            translation_model text not null,
            translation_status text not null,
            translated_at text not null,
            error_message text,
            primary key (market_id, language)
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
            previous_event_hash text,
            event_hash text,
            integrity_payload_json text,
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
    _ensure_column(conn, "demo_audit_events", "previous_event_hash", "text")
    _ensure_column(conn, "demo_audit_events", "event_hash", "text")
    _ensure_column(conn, "demo_audit_events", "integrity_payload_json", "text")
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


def normalize_demo_user_id(value: str | None) -> str:
    text = (value or DEMO_USER_ID).strip()
    if not text:
        text = DEMO_USER_ID
    normalized = USER_ID_PATTERN.sub("-", text)[:40].strip(".-_")
    return normalized or DEMO_USER_ID


def ensure_demo_user(conn: sqlite3.Connection, user_id: str) -> str:
    normalized = normalize_demo_user_id(user_id)
    existing = conn.execute("select user_id from demo_users where user_id = ?", (normalized,)).fetchone()
    if existing is None:
        conn.execute("insert into demo_users(user_id, balance) values (?, ?)", (normalized, INITIAL_DEMO_POINTS))
        conn.execute(
            """
            insert into demo_point_ledger(user_id, amount, balance_before, balance_after, entry_type, note)
            values (?, ?, ?, ?, ?, ?)
            """,
            (normalized, INITIAL_DEMO_POINTS, 0.0, INITIAL_DEMO_POINTS, "initial", "initial demo points"),
        )
        conn.commit()
    return normalized


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


def list_markets_for_translation(conn: sqlite3.Connection, limit: int, market_id: str | None = None) -> list[dict[str, Any]]:
    if market_id:
        market = get_market(conn, market_id)
        return [market] if market else []
    rows = conn.execute(
        "select payload from markets order by updated_at desc, market_id asc limit ?",
        (max(1, min(int(limit), 50)),),
    ).fetchall()
    return [json.loads(row["payload"]) for row in rows]


CATALOG_SORT_SQL = {
    "volume_24h": "cast(coalesce(json_extract(payload, '$.volume_24hr'), 0) as real)",
    "liquidity": "cast(coalesce(json_extract(payload, '$.liquidity'), 0) as real)",
    "end_date": "coalesce(datetime(json_extract(payload, '$.end_date')), '')",
    "probability": "cast(coalesce((select value from json_each(markets.payload, '$.probabilities') where key = json_extract(markets.payload, '$.outcomes[0]') limit 1), 0) as real)",
    "updated": "updated_at",
}


def _catalog_like_query(query: str) -> str:
    escaped = query.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped.lower()}%"


def list_market_catalog(
    conn: sqlite3.Connection,
    query: str,
    status: str,
    sort: str,
    order: str,
    limit: int,
    offset: int,
) -> dict[str, Any]:
    """Return one user-facing market catalog page with SQL-side filtering and paging."""
    normalized_status = status if status in {"active", "closed", "all"} else "active"
    sort_sql = CATALOG_SORT_SQL.get(sort, CATALOG_SORT_SQL["volume_24h"])
    order_sql = "asc" if order == "asc" else "desc"
    limit = max(1, min(int(limit), 50))
    offset = max(0, int(offset))

    active_sql = """
        coalesce(json_extract(payload, '$.active'), 0) = 1
        and coalesce(json_extract(payload, '$.closed'), 0) = 0
        and (json_extract(payload, '$.end_date') is null
             or datetime(json_extract(payload, '$.end_date')) > datetime('now'))
    """
    closed_sql = f"not ({active_sql})"
    where: list[str] = []
    params: list[Any] = []
    if normalized_status == "active":
        where.append(active_sql)
    elif normalized_status == "closed":
        where.append(closed_sql)
    if query:
        where.append(
            "("
            "lower(coalesce(json_extract(payload, '$.title'), '')) like ? escape '\\' "
            "or lower(coalesce(json_extract(payload, '$.question'), '')) like ? escape '\\' "
            "or lower(coalesce(json_extract(payload, '$.slug'), '')) like ? escape '\\'"
            ")"
        )
        like_query = _catalog_like_query(query)
        params.extend([like_query, like_query, like_query])

    where_sql = f" where {' and '.join(where)}" if where else ""
    total_count = int(conn.execute(f"select count(*) as count from markets{where_sql}", params).fetchone()["count"])
    rows = conn.execute(
        f"select payload from markets{where_sql} order by {sort_sql} {order_sql}, market_id asc limit ? offset ?",
        [*params, limit, offset],
    ).fetchall()
    return {
        "markets": [json.loads(row["payload"]) for row in rows],
        "total_count": total_count,
        "total_pages": math.ceil(total_count / limit) if total_count else 0,
    }


def get_last_fetch_run(conn: sqlite3.Connection) -> dict[str, Any] | None:
    row = conn.execute("select * from fetch_runs order by id desc limit 1").fetchone()
    return dict(row) if row else None


def get_market(conn: sqlite3.Connection, market_id: str) -> dict[str, Any] | None:
    row = conn.execute("select payload from markets where market_id = ?", (market_id,)).fetchone()
    return json.loads(row["payload"]) if row else None


def list_markets_by_ids(conn: sqlite3.Connection, market_ids: list[str]) -> list[dict[str, Any]]:
    if not market_ids:
        return []
    placeholders = ", ".join("?" for _ in market_ids)
    rows = conn.execute(
        f"select market_id, payload from markets where market_id in ({placeholders})",
        market_ids,
    ).fetchall()
    by_id = {str(row["market_id"]): json.loads(row["payload"]) for row in rows}
    return [by_id[market_id] for market_id in market_ids if market_id in by_id]


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


def get_ledger_entry(conn: sqlite3.Connection, ledger_entry_id: int) -> dict[str, Any] | None:
    row = conn.execute("select * from demo_point_ledger where id = ?", (ledger_entry_id,)).fetchone()
    return dict(row) if row else None


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


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _latest_audit_event_hash(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        """
        select event_hash from demo_audit_events
        where event_hash is not null and event_hash != ''
        order by id desc limit 1
        """
    ).fetchone()
    return str(row["event_hash"]) if row else ""


def _audit_integrity_payload(
    *,
    event_type: str,
    user_id: str | None,
    route: str | None,
    request_id: str | None,
    reference_type: str | None,
    reference_id: str | None,
    before_json: str | None,
    after_json: str | None,
    note: str | None,
    created_at: str,
) -> dict[str, Any]:
    return {
        "after_json": after_json,
        "before_json": before_json,
        "created_at": created_at,
        "event_type": event_type,
        "note": note,
        "reference_id": reference_id,
        "reference_type": reference_type,
        "request_id": request_id,
        "route": route,
        "user_id": user_id,
    }


def _audit_event_hash(previous_event_hash: str, integrity_payload_json: str) -> str:
    return hashlib.sha256((previous_event_hash + integrity_payload_json).encode("utf-8")).hexdigest()


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
    reference_id_text = str(reference_id) if reference_id is not None else None
    before_json = json.dumps(before, ensure_ascii=False) if before is not None else None
    after_json = json.dumps(after, ensure_ascii=False) if after is not None else None
    created_at = datetime.now(timezone.utc).isoformat()
    previous_event_hash = _latest_audit_event_hash(conn)
    integrity_payload_json = _canonical_json(
        _audit_integrity_payload(
            event_type=event_type,
            user_id=user_id,
            route=route,
            request_id=request_id,
            reference_type=reference_type,
            reference_id=reference_id_text,
            before_json=before_json,
            after_json=after_json,
            note=note,
            created_at=created_at,
        )
    )
    event_hash = _audit_event_hash(previous_event_hash, integrity_payload_json)
    cursor = conn.execute(
        """
        insert into demo_audit_events(
            event_type, user_id, route, request_id, reference_type, reference_id,
            before_json, after_json, note, previous_event_hash, event_hash,
            integrity_payload_json, created_at
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (
            event_type,
            user_id,
            route,
            request_id,
            reference_type,
            reference_id_text,
            before_json,
            after_json,
            note,
            previous_event_hash,
            event_hash,
            integrity_payload_json,
            created_at,
        ),
    )
    row = conn.execute("select * from demo_audit_events where id = ?", (cursor.lastrowid,)).fetchone()
    return dict(row)


def verify_audit_chain(conn: sqlite3.Connection) -> dict[str, Any]:
    rows = conn.execute("select * from demo_audit_events order by id asc").fetchall()
    checked_count = len(rows)
    verified_count = 0
    missing_hash_count = 0
    broken_count = 0
    first_broken_event_id: int | None = None
    expected_previous_hash = ""

    for row in rows:
        event_hash = row["event_hash"]
        previous_event_hash = row["previous_event_hash"]
        integrity_payload_json = row["integrity_payload_json"]
        if not event_hash or not integrity_payload_json:
            missing_hash_count += 1
            continue

        reference_id = str(row["reference_id"]) if row["reference_id"] is not None else None
        expected_payload_json = _canonical_json(
            _audit_integrity_payload(
                event_type=row["event_type"],
                user_id=row["user_id"],
                route=row["route"],
                request_id=row["request_id"],
                reference_type=row["reference_type"],
                reference_id=reference_id,
                before_json=row["before_json"],
                after_json=row["after_json"],
                note=row["note"],
                created_at=row["created_at"],
            )
        )
        expected_event_hash = _audit_event_hash(previous_event_hash or "", integrity_payload_json)
        valid = (
            integrity_payload_json == expected_payload_json
            and (previous_event_hash or "") == expected_previous_hash
            and event_hash == expected_event_hash
        )
        if valid:
            verified_count += 1
            expected_previous_hash = event_hash
            continue
        broken_count += 1
        if first_broken_event_id is None:
            first_broken_event_id = int(row["id"])

    if checked_count == 0:
        integrity_status = "empty"
    elif broken_count:
        integrity_status = "broken"
    elif missing_hash_count:
        integrity_status = "partial_legacy_rows"
    else:
        integrity_status = "verified"
    return {
        "checked_count": checked_count,
        "verified_count": verified_count,
        "missing_hash_count": missing_hash_count,
        "broken_count": broken_count,
        "first_broken_event_id": first_broken_event_id,
        "integrity_status": integrity_status,
    }


def list_audit_events(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        "select * from demo_audit_events order by id desc limit ?",
        (limit,),
    ).fetchall()
    return [dict(row) for row in rows]


def list_admin_audit_events(
    conn: sqlite3.Connection,
    *,
    user_id: str | None = None,
    event_type: str | None = None,
    reference_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "select * from demo_audit_events where 1 = 1"
    params: list[Any] = []
    if user_id:
        sql += " and user_id = ?"
        params.append(user_id)
    if event_type:
        sql += " and event_type = ?"
        params.append(event_type)
    if reference_id:
        sql += " and reference_id = ?"
        params.append(reference_id)
    if date_from:
        sql += " and created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " and created_at <= ?"
        params.append(date_to)
    sql += " order by id desc limit ? offset ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_admin_ledger_entries(
    conn: sqlite3.Connection,
    *,
    user_id: str | None = None,
    market_id: str | None = None,
    reference_id: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "select * from demo_point_ledger where 1 = 1"
    params: list[Any] = []
    if user_id:
        sql += " and user_id = ?"
        params.append(user_id)
    if market_id:
        sql += " and market_id = ?"
        params.append(market_id)
    if reference_id:
        sql += " and reference_id = ?"
        params.append(reference_id)
    if date_from:
        sql += " and created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " and created_at <= ?"
        params.append(date_to)
    sql += " order by id desc limit ? offset ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_admin_settlements(
    conn: sqlite3.Connection,
    *,
    user_id: str | None = None,
    market_id: str | None = None,
    position_id: str | None = None,
    settled: str | None = None,
    date_from: str | None = None,
    date_to: str | None = None,
    limit: int = 100,
    offset: int = 0,
) -> list[dict[str, Any]]:
    sql = "select * from demo_settlements where 1 = 1"
    params: list[Any] = []
    if user_id:
        sql += " and user_id = ?"
        params.append(user_id)
    if market_id:
        sql += " and market_id = ?"
        params.append(market_id)
    if position_id:
        sql += " and position_id = ?"
        params.append(position_id)
    if settled == "settled":
        sql += " and status in ('settled_win', 'settled_loss')"
    elif settled == "unsettled":
        sql += " and status not in ('settled_win', 'settled_loss')"
    if date_from:
        sql += " and created_at >= ?"
        params.append(date_from)
    if date_to:
        sql += " and created_at <= ?"
        params.append(date_to)
    sql += " order by id desc limit ? offset ?"
    params.extend([limit, offset])
    rows = conn.execute(sql, params).fetchall()
    return [dict(row) for row in rows]


def list_demo_user_overview(conn: sqlite3.Connection, limit: int = 100) -> list[dict[str, Any]]:
    rows = conn.execute(
        """
        select
            user_id,
            balance,
            (select count(*) from simulated_positions where simulated_positions.user_id = demo_users.user_id) as position_count,
            (select count(*) from demo_point_ledger where demo_point_ledger.user_id = demo_users.user_id) as history_count,
            (select count(*) from demo_settlements where demo_settlements.user_id = demo_users.user_id) as result_count
        from demo_users
        order by user_id
        limit ?
        """,
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
            "明確な結果をまだ確認できていません。",
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
