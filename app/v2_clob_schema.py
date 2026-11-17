"""Schema-v2 definitions and read-only validation; no migration entry point."""

from __future__ import annotations

import re
import sqlite3
from functools import lru_cache
from collections import Counter

from app.storage import CURRENT_SCHEMA_COLUMNS, CURRENT_SCHEMA_REQUIRED_INDEXES, CURRENT_SCHEMA_REQUIRED_TABLES

SCHEMA_VERSION = 2
MAX_INT = 9223372036854775807
ENGINE = "collateralized_clob_v2"

# These statements are definitions only. Callers must not use them to replace
# populated tables; the fixture-only installation is deliberately in tests.
TABLES = {
    "order_collateral_reservations": """CREATE TABLE order_collateral_reservations (
        id INTEGER PRIMARY KEY AUTOINCREMENT, engine_key TEXT NOT NULL REFERENCES prediction_engines(engine_key),
        account_id TEXT NOT NULL REFERENCES point_accounts(account_id), participant_id TEXT NOT NULL,
        market_id TEXT NOT NULL REFERENCES collateral_markets(market_id), side TEXT NOT NULL CHECK(side IN ('BUY','SELL')),
        outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')), quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0),
        limit_price_micro INTEGER NOT NULL CHECK(typeof(limit_price_micro)='integer' AND limit_price_micro BETWEEN 1 AND 10000),
        collateral_type TEXT NOT NULL CHECK(collateral_type IN ('point','share')),
        collateral_amount INTEGER NOT NULL CHECK(typeof(collateral_amount)='integer' AND collateral_amount>0),
        status TEXT NOT NULL CHECK(status IN ('reserved','released','consumed')),
        release_reason TEXT, version INTEGER NOT NULL DEFAULT 0 CHECK(typeof(version)='integer' AND version>=0),
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, released_at TEXT,
        contract_version INTEGER NOT NULL CHECK(typeof(contract_version)='integer' AND contract_version IN (1,2)),
        remaining_collateral INTEGER NOT NULL CHECK(typeof(remaining_collateral)='integer' AND remaining_collateral>=0),
        consumed_collateral INTEGER NOT NULL CHECK(typeof(consumed_collateral)='integer' AND consumed_collateral>=0),
        released_collateral INTEGER NOT NULL CHECK(typeof(released_collateral)='integer' AND released_collateral>=0), consumed_at TEXT,
        CHECK((side='BUY' AND collateral_type='point' AND quantity<=9223372036854775807/limit_price_micro AND collateral_amount=quantity*limit_price_micro) OR (side='SELL' AND collateral_type='share' AND collateral_amount=quantity)),
        CHECK(remaining_collateral<=collateral_amount AND consumed_collateral<=collateral_amount-remaining_collateral AND released_collateral=collateral_amount-remaining_collateral-consumed_collateral),
        CHECK((contract_version=1 AND consumed_collateral=0 AND consumed_at IS NULL AND ((status='reserved' AND remaining_collateral=collateral_amount AND released_collateral=0 AND release_reason IS NULL AND released_at IS NULL) OR (status='released' AND remaining_collateral=0 AND released_collateral=collateral_amount AND release_reason IS NOT NULL AND release_reason IN ('cancelled','rejected') AND released_at IS NOT NULL))) OR
          (contract_version=2 AND ((status='reserved' AND remaining_collateral>0 AND release_reason IS NULL AND released_at IS NULL AND consumed_at IS NULL) OR (status='released' AND remaining_collateral=0 AND released_collateral>0 AND release_reason IS NOT NULL AND release_reason IN ('cancelled','rejected','expired','market_frozen','self_trade_prevention','fill_limit') AND released_at IS NOT NULL AND consumed_at IS NULL) OR (status='consumed' AND remaining_collateral=0 AND released_collateral=0 AND release_reason IS NULL AND released_at IS NULL AND consumed_at IS NOT NULL))))
    )""",
    "order_collateral_events": """CREATE TABLE order_collateral_events (
        id INTEGER PRIMARY KEY AUTOINCREMENT, engine_key TEXT NOT NULL REFERENCES prediction_engines(engine_key),
        reservation_id INTEGER NOT NULL REFERENCES order_collateral_reservations(id), account_id TEXT NOT NULL REFERENCES point_accounts(account_id),
        event_type TEXT NOT NULL CHECK(event_type IN ('reserve','release','consume')), release_reason TEXT,
        asset_type TEXT NOT NULL CHECK(asset_type IN ('point','share')), asset_amount INTEGER NOT NULL CHECK(typeof(asset_amount)='integer' AND asset_amount>0),
        available_before INTEGER NOT NULL CHECK(typeof(available_before)='integer' AND available_before>=0), available_after INTEGER NOT NULL CHECK(typeof(available_after)='integer' AND available_after>=0),
        locked_before INTEGER NOT NULL CHECK(typeof(locked_before)='integer' AND locked_before>=0), locked_after INTEGER NOT NULL CHECK(typeof(locked_after)='integer' AND locked_after>=0),
        idempotency_key TEXT, request_id TEXT, payload_hash TEXT NOT NULL, created_at TEXT NOT NULL,
        trade_id INTEGER REFERENCES v2_trades(id), event_role TEXT,
        reservation_before INTEGER, reservation_after INTEGER, consumed_before INTEGER, consumed_after INTEGER,
        UNIQUE(engine_key,account_id,idempotency_key), UNIQUE(trade_id,event_role),
        CHECK((event_type IN ('reserve','release') AND idempotency_key IS NOT NULL AND trade_id IS NULL AND event_role IS NULL AND reservation_before IS NULL AND reservation_after IS NULL AND consumed_before IS NULL AND consumed_after IS NULL AND
          ((event_type='reserve' AND release_reason IS NULL AND available_before>=asset_amount AND available_after=available_before-asset_amount AND locked_after>=locked_before AND locked_after-locked_before=asset_amount) OR
           (event_type='release' AND release_reason IS NOT NULL AND release_reason IN ('cancelled','rejected','expired','market_frozen','self_trade_prevention','fill_limit') AND available_after>=available_before AND available_after-available_before=asset_amount AND locked_before>=asset_amount AND locked_after=locked_before-asset_amount))) OR
          (event_type='consume' AND idempotency_key IS NULL AND release_reason IS NULL AND trade_id IS NOT NULL AND event_role IS NOT NULL AND event_role IN ('BUY_CONSUME','SELL_CONSUME') AND length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*' AND
           typeof(reservation_before)='integer' AND typeof(reservation_after)='integer' AND typeof(consumed_before)='integer' AND typeof(consumed_after)='integer' AND reservation_after>=0 AND consumed_before>=0 AND reservation_before>=reservation_after AND reservation_before-reservation_after=asset_amount AND consumed_after>=consumed_before AND consumed_after-consumed_before=asset_amount))
    )""",
    "order_collateral_ledger_entries": """CREATE TABLE order_collateral_ledger_entries (
        id INTEGER PRIMARY KEY AUTOINCREMENT, engine_key TEXT NOT NULL REFERENCES prediction_engines(engine_key), reservation_id INTEGER NOT NULL REFERENCES order_collateral_reservations(id), event_id INTEGER NOT NULL REFERENCES order_collateral_events(id),
        account_id TEXT NOT NULL REFERENCES point_accounts(account_id), market_id TEXT NOT NULL REFERENCES collateral_markets(market_id), outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')),
        asset_type TEXT NOT NULL CHECK(asset_type IN ('point','share')), balance_bucket TEXT NOT NULL CHECK(balance_bucket IN ('available','locked')),
        delta INTEGER NOT NULL CHECK(typeof(delta)='integer' AND delta<>0), balance_before INTEGER NOT NULL CHECK(typeof(balance_before)='integer' AND balance_before>=0), balance_after INTEGER NOT NULL CHECK(typeof(balance_after)='integer' AND balance_after>=0), request_id TEXT, created_at TEXT NOT NULL,
        CHECK((delta>0 AND balance_after>=balance_before AND balance_after-balance_before=delta) OR (delta<0 AND balance_before>=balance_after AND balance_after-balance_before=delta)), UNIQUE(event_id,balance_bucket)
    )""",
    "v2_orders": """CREATE TABLE v2_orders (
        id INTEGER PRIMARY KEY, reservation_id INTEGER NOT NULL UNIQUE REFERENCES order_collateral_reservations(id), engine_key TEXT NOT NULL CHECK(engine_key='collateralized_clob_v2'),
        account_id TEXT NOT NULL REFERENCES point_accounts(account_id), participant_id TEXT NOT NULL CHECK(length(participant_id)>0), market_id TEXT NOT NULL REFERENCES collateral_markets(market_id), side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')),
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity BETWEEN 1 AND 1000000), filled_quantity INTEGER NOT NULL CHECK(typeof(filled_quantity)='integer' AND filled_quantity>=0), unfilled_quantity INTEGER NOT NULL CHECK(typeof(unfilled_quantity)='integer' AND unfilled_quantity>=0), fill_count INTEGER NOT NULL CHECK(typeof(fill_count)='integer' AND fill_count BETWEEN 0 AND 1000),
        limit_price_micro INTEGER NOT NULL CHECK(typeof(limit_price_micro)='integer' AND limit_price_micro BETWEEN 1 AND 10000), status TEXT NOT NULL CHECK(status IN ('OPEN','PARTIALLY_FILLED','FILLED','CANCELLED','EXPIRED','REJECTED')), terminal_reason TEXT,
        priority_seq INTEGER NOT NULL UNIQUE CHECK(typeof(priority_seq)='integer' AND priority_seq>0), expires_at_us INTEGER CHECK(expires_at_us IS NULL OR (typeof(expires_at_us)='integer' AND expires_at_us>=0)), created_at_us INTEGER NOT NULL CHECK(typeof(created_at_us)='integer' AND created_at_us>=0), updated_at_us INTEGER NOT NULL CHECK(typeof(updated_at_us)='integer' AND updated_at_us>=0), version INTEGER NOT NULL CHECK(typeof(version)='integer' AND version>=0),
        CHECK(filled_quantity<=quantity AND unfilled_quantity=quantity-filled_quantity),
        CHECK((status='OPEN' AND filled_quantity=0 AND unfilled_quantity>0 AND terminal_reason IS NULL) OR (status='PARTIALLY_FILLED' AND filled_quantity>0 AND unfilled_quantity>0 AND terminal_reason IS NULL) OR (status='FILLED' AND filled_quantity=quantity AND unfilled_quantity=0 AND terminal_reason IS NOT NULL AND terminal_reason='fully_filled') OR (status='CANCELLED' AND unfilled_quantity>0 AND terminal_reason IS NOT NULL AND terminal_reason IN ('cancelled','self_trade_prevention','market_frozen','fill_limit')) OR (status='EXPIRED' AND unfilled_quantity>0 AND terminal_reason IS NOT NULL AND terminal_reason='expired') OR (status='REJECTED' AND filled_quantity=0 AND unfilled_quantity=quantity AND terminal_reason IS NOT NULL AND terminal_reason='rejected'))
    )""",
    "v2_trades": """CREATE TABLE v2_trades (
        id INTEGER PRIMARY KEY, engine_key TEXT NOT NULL CHECK(engine_key='collateralized_clob_v2'), market_id TEXT NOT NULL REFERENCES collateral_markets(market_id), outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')),
        buy_order_id INTEGER NOT NULL REFERENCES v2_orders(id), sell_order_id INTEGER NOT NULL REFERENCES v2_orders(id), maker_order_id INTEGER NOT NULL REFERENCES v2_orders(id), taker_order_id INTEGER NOT NULL REFERENCES v2_orders(id),
        quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0), price_micro INTEGER NOT NULL CHECK(typeof(price_micro)='integer' AND price_micro BETWEEN 1 AND 10000), execution_key TEXT NOT NULL UNIQUE,
        taker_fill_ordinal INTEGER NOT NULL CHECK(typeof(taker_fill_ordinal)='integer' AND taker_fill_ordinal BETWEEN 1 AND 1000), created_at_us INTEGER NOT NULL CHECK(typeof(created_at_us)='integer' AND created_at_us>=0),
        UNIQUE(taker_order_id,taker_fill_ordinal), CHECK(buy_order_id<>sell_order_id AND maker_order_id<>taker_order_id AND ((maker_order_id=buy_order_id AND taker_order_id=sell_order_id) OR (maker_order_id=sell_order_id AND taker_order_id=buy_order_id))), CHECK(execution_key='v2:'||taker_order_id||':'||taker_fill_ordinal)
    )""",
    "v2_fills": """CREATE TABLE v2_fills (id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL REFERENCES v2_trades(id), order_id INTEGER NOT NULL REFERENCES v2_orders(id), side TEXT NOT NULL CHECK(side IN ('BUY','SELL')), quantity INTEGER NOT NULL CHECK(typeof(quantity)='integer' AND quantity>0), price_micro INTEGER NOT NULL CHECK(typeof(price_micro)='integer' AND price_micro BETWEEN 1 AND 10000), created_at_us INTEGER NOT NULL CHECK(typeof(created_at_us)='integer' AND created_at_us>=0), UNIQUE(trade_id,order_id), UNIQUE(trade_id,side))""",
    "v2_trade_ledger_entries": """CREATE TABLE v2_trade_ledger_entries (id INTEGER PRIMARY KEY, trade_id INTEGER NOT NULL REFERENCES v2_trades(id), account_id TEXT NOT NULL REFERENCES point_accounts(account_id), market_id TEXT NOT NULL REFERENCES collateral_markets(market_id), outcome TEXT NOT NULL CHECK(outcome IN ('YES','NO')), role TEXT NOT NULL CHECK(role IN ('BUY_LOCKED','BUY_REFUND','SELL_PROCEEDS','SELL_SHARES','BUY_SHARES')), asset_type TEXT NOT NULL CHECK(asset_type IN ('point','share')), bucket TEXT NOT NULL CHECK(bucket IN ('available','locked')), delta INTEGER NOT NULL CHECK(typeof(delta)='integer'), balance_before INTEGER NOT NULL CHECK(typeof(balance_before)='integer' AND balance_before>=0), balance_after INTEGER NOT NULL CHECK(typeof(balance_after)='integer' AND balance_after>=0), created_at_us INTEGER NOT NULL CHECK(typeof(created_at_us)='integer' AND created_at_us>=0), UNIQUE(trade_id,role), CHECK(delta<>0 OR role='BUY_REFUND'), CHECK((delta>=0 AND balance_after>=balance_before AND balance_after-balance_before=delta) OR (delta<0 AND balance_before>=balance_after AND balance_after-balance_before=delta)), CHECK((role='BUY_LOCKED' AND asset_type='point' AND bucket='locked' AND delta<0) OR (role='BUY_REFUND' AND asset_type='point' AND bucket='available' AND delta>=0) OR (role='SELL_PROCEEDS' AND asset_type='point' AND bucket='available' AND delta>0) OR (role='SELL_SHARES' AND asset_type='share' AND bucket='locked' AND delta<0) OR (role='BUY_SHARES' AND asset_type='share' AND bucket='available' AND delta>0)))""",
    "v2_command_results": """CREATE TABLE v2_command_results (id INTEGER PRIMARY KEY, engine_key TEXT NOT NULL CHECK(engine_key='collateralized_clob_v2'), actor_id TEXT NOT NULL CHECK(length(actor_id)>0), operation TEXT NOT NULL CHECK(operation IN ('place','cancel','reject')), idempotency_key TEXT NOT NULL CHECK(length(idempotency_key)>0), payload_hash TEXT NOT NULL CHECK(length(payload_hash)=64 AND payload_hash NOT GLOB '*[^0-9a-f]*'), response_status INTEGER NOT NULL CHECK(typeof(response_status)='integer'), response_json TEXT NOT NULL, created_at_us INTEGER NOT NULL CHECK(typeof(created_at_us)='integer' AND created_at_us>=0), UNIQUE(engine_key,actor_id,operation,idempotency_key))""",
    "v2_priority_sequence": """CREATE TABLE v2_priority_sequence (id INTEGER PRIMARY KEY CHECK(id=1), last_value INTEGER NOT NULL CHECK(typeof(last_value)='integer' AND last_value>=0))""",
}

INDEXES = {
    'idx_order_collateral_reservation_account': 'CREATE INDEX idx_order_collateral_reservation_account ON order_collateral_reservations(account_id,created_at,id)',
    'idx_order_collateral_reservation_participant': 'CREATE INDEX idx_order_collateral_reservation_participant ON order_collateral_reservations(participant_id,created_at,id)',
    'idx_order_collateral_reservation_market': 'CREATE INDEX idx_order_collateral_reservation_market ON order_collateral_reservations(market_id,created_at,id)',
    'idx_order_collateral_event_reservation': 'CREATE INDEX idx_order_collateral_event_reservation ON order_collateral_events(reservation_id)',
    'idx_order_collateral_event_account': 'CREATE INDEX idx_order_collateral_event_account ON order_collateral_events(account_id)',
    'idx_order_collateral_ledger_reservation': 'CREATE INDEX idx_order_collateral_ledger_reservation ON order_collateral_ledger_entries(reservation_id)',
    'idx_order_collateral_ledger_event': 'CREATE INDEX idx_order_collateral_ledger_event ON order_collateral_ledger_entries(event_id)',
    'idx_order_collateral_ledger_account': 'CREATE INDEX idx_order_collateral_ledger_account ON order_collateral_ledger_entries(account_id)',
    'idx_order_collateral_ledger_market': 'CREATE INDEX idx_order_collateral_ledger_market ON order_collateral_ledger_entries(market_id)',
    'idx_v2_book_buy': "CREATE INDEX idx_v2_book_buy ON v2_orders(market_id,outcome,side,limit_price_micro DESC,priority_seq) WHERE status IN ('OPEN','PARTIALLY_FILLED')",
    'idx_v2_book_sell': "CREATE INDEX idx_v2_book_sell ON v2_orders(market_id,outcome,side,limit_price_micro ASC,priority_seq) WHERE status IN ('OPEN','PARTIALLY_FILLED')",
    'idx_v2_expiry': 'CREATE INDEX idx_v2_expiry ON v2_orders(status,expires_at_us,priority_seq)',
    'idx_v2_fills_order': 'CREATE INDEX idx_v2_fills_order ON v2_fills(order_id,id)',
    'idx_v2_trades_market': 'CREATE INDEX idx_v2_trades_market ON v2_trades(market_id,outcome,id)',
    'idx_v2_trade_ledger_account': 'CREATE INDEX idx_v2_trade_ledger_account ON v2_trade_ledger_entries(account_id,market_id,id)',
    'idx_v2_reservation_buy': 'CREATE INDEX idx_v2_reservation_buy ON order_collateral_reservations(account_id,status,side)',
    'idx_v2_reservation_sell': 'CREATE INDEX idx_v2_reservation_sell ON order_collateral_reservations(account_id,market_id,outcome,status,side)',
    'idx_v2_event_order': 'CREATE INDEX idx_v2_event_order ON order_collateral_events(reservation_id,id)',
}

TRIGGERS = {
    'v2_reservation_immutable': """CREATE TRIGGER v2_reservation_immutable
        BEFORE UPDATE OF engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,contract_version ON order_collateral_reservations
        WHEN NEW.engine_key IS NOT OLD.engine_key OR NEW.account_id IS NOT OLD.account_id OR NEW.participant_id IS NOT OLD.participant_id OR NEW.market_id IS NOT OLD.market_id OR NEW.side IS NOT OLD.side OR NEW.outcome IS NOT OLD.outcome OR NEW.quantity IS NOT OLD.quantity OR NEW.limit_price_micro IS NOT OLD.limit_price_micro OR NEW.collateral_type IS NOT OLD.collateral_type OR NEW.collateral_amount IS NOT OLD.collateral_amount OR NEW.contract_version IS NOT OLD.contract_version
        BEGIN SELECT RAISE(ABORT,'reservation immutable'); END""",
    'v2_legacy_event_guard': """CREATE TRIGGER v2_legacy_event_guard
        BEFORE INSERT ON order_collateral_events
        WHEN EXISTS (SELECT 1 FROM order_collateral_reservations r WHERE r.id=NEW.reservation_id AND r.contract_version=1) AND (NEW.event_type='consume' OR (NEW.event_type='release' AND NEW.release_reason NOT IN ('cancelled','rejected')))
        BEGIN SELECT RAISE(ABORT,'legacy event contract'); END""",
    'v2_event_immutable': """CREATE TRIGGER v2_event_immutable
        BEFORE UPDATE ON order_collateral_events
        BEGIN SELECT RAISE(ABORT,'event immutable'); END""",
}


def _canonical(sql: str) -> tuple[str, ...]:
    # Preserve literal case: changing 'BUY' to 'buy' changes the contract.
    normalized = ' '.join(sql.split())
    # SQLite quotes only the table identifier after ALTER TABLE ... RENAME.
    normalized = re.sub(r'^CREATE TABLE "([A-Za-z_][A-Za-z_0-9]*)"', r'CREATE TABLE \1', normalized)
    # Tokenization ignores whitespace around punctuation without touching literals.
    return tuple(re.findall(r"'(?:''|[^'])*'|\"(?:\"\"|[^\"])*\"|<=|>=|<>|!=|==|\|\||[A-Za-z_][A-Za-z_0-9]*|[0-9]+|[^\s]", normalized))


def _quote_identifier(name):
    return '"' + name.replace('"', '""') + '"'


def _checks(sql):
    tokens = _canonical(sql)
    found = []
    i = 0
    while i < len(tokens):
        if tokens[i].lower() == 'check' and i + 1 < len(tokens) and tokens[i + 1] == '(':
            start = i
            i += 1
            depth = 0
            while i < len(tokens):
                if tokens[i] == '(':
                    depth += 1
                elif tokens[i] == ')':
                    depth -= 1
                    if depth == 0:
                        found.append(tuple(token if token.startswith(("'", '"')) else token.lower()
                                           for token in tokens[start:i + 1]))
                        break
                i += 1
            if depth != 0:
                raise ValueError('schema_invalid_check')
        i += 1
    return Counter(found)


def _columns(conn, table):
    return {row[1]: (row[2].upper(), row[3], row[5], row[6])
            for row in conn.execute('PRAGMA table_xinfo(' + _quote_identifier(table) + ')')}


def _foreign_keys(conn, table):
    return sorted(tuple(row[2:]) for row in conn.execute('PRAGMA foreign_key_list(' + _quote_identifier(table) + ')'))


def _index_signature(conn, name):
    return tuple((row[2] if row[2] is not None else row[1], row[3], row[4], row[5])
                 for row in conn.execute('PRAGMA index_xinfo(' + _quote_identifier(name) + ')'))


def _unique_indexes(conn, table):
    return sorted((_index_signature(conn, row[1])
                   for row in conn.execute('PRAGMA index_list(' + _quote_identifier(table) + ')') if row[2]), key=repr)


@lru_cache(maxsize=1)
def _legacy_reference():
    # Build a private in-memory reference through the existing V1 schema path.
    # The examined database itself is never changed.
    from app.storage import connect, init_db
    reference = connect(':memory:')
    try:
        init_db(reference)
        tables = {}
        for table in CURRENT_SCHEMA_REQUIRED_TABLES - TABLES.keys():
            sql = reference.execute("SELECT sql FROM sqlite_master WHERE type='table' AND name=?", (table,)).fetchone()[0]
            tables[table] = (_columns(reference, table), _foreign_keys(reference, table), _unique_indexes(reference, table), _checks(sql))
        indexes = {}
        for row in reference.execute("SELECT name,tbl_name FROM sqlite_master WHERE type='index' AND sql IS NOT NULL"):
            name, table = row
            if name in CURRENT_SCHEMA_REQUIRED_INDEXES and name not in INDEXES:
                info = next(item for item in reference.execute('PRAGMA index_list(' + _quote_identifier(table) + ')') if item[1] == name)
                indexes[name] = (table, info[2], info[4], _index_signature(reference, name))
        return tables, indexes
    finally:
        reference.close()


def _validate_inherited_v1(conn, actual):
    tables, indexes = _legacy_reference()
    for table, (expected, foreign_keys, unique_indexes, checks) in tables.items():
        columns = _columns(conn, table)
        # Existing V1 validation permits legacy column order; preserve that.
        if not expected.keys() <= columns.keys() or any(columns[name] != info for name, info in expected.items()):
            raise ValueError(f'schema_invalid_table:{table}')
        if table in CURRENT_SCHEMA_COLUMNS and set(columns) != set(CURRENT_SCHEMA_COLUMNS[table]):
            raise ValueError(f'schema_invalid_table:{table}')
        if (_foreign_keys(conn, table) != foreign_keys or _unique_indexes(conn, table) != unique_indexes
                or checks - _checks(actual[('table', table)])):
            raise ValueError(f'schema_invalid_table:{table}')
    for name, (table, unique, partial, signature) in indexes.items():
        row = next((item for item in conn.execute('PRAGMA index_list(' + _quote_identifier(table) + ')') if item[1] == name), None)
        if row is None or row[2] != unique or row[4] != partial or _index_signature(conn, name) != signature:
            raise ValueError(f'schema_invalid_index:{name}')


def validate_v2_schema(conn: sqlite3.Connection) -> None:
    """Fail closed on altered schema. Read-only; does not mutate connection state."""
    if conn.execute('PRAGMA user_version').fetchone()[0] != SCHEMA_VERSION:
        raise ValueError('schema_version_mismatch')
    rows = conn.execute("SELECT type,name,sql FROM sqlite_master WHERE type IN ('table','index','trigger')").fetchall()
    actual = {(row[0], row[1]): row[2] for row in rows}
    if not CURRENT_SCHEMA_REQUIRED_TABLES.issubset({n for t, n in actual if t == 'table'}):
        raise ValueError('schema_missing_tables')
    if not CURRENT_SCHEMA_REQUIRED_INDEXES.issubset({n for t, n in actual if t == 'index'}):
        raise ValueError('schema_missing_indexes')
    for kind, definitions in (('table', TABLES), ('index', INDEXES), ('trigger', TRIGGERS)):
        for name, definition in definitions.items():
            value = actual.get((kind, name))
            if value is None or _canonical(value) != _canonical(definition):
                raise ValueError(f'schema_invalid_{kind}:{name}')
    _validate_inherited_v1(conn, actual)
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
        raise ValueError('integrity_failed')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None:
        raise ValueError('foreign_key_failed')
    rows = conn.execute('SELECT id,last_value,typeof(last_value) FROM v2_priority_sequence').fetchall()
    if len(rows) != 1 or rows[0][0] != 1 or rows[0][2] != 'integer' or rows[0][1] < 0:
        raise ValueError('priority_sequence_invalid')
