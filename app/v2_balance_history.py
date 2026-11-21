"""Opt-in V2 balance history for isolated migration fixtures.

Bootstrap, allocation, market creation, split and merge are integrated when
history is explicitly installed. Other writers remain unsupported.
"""
from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid


class HistoryError(RuntimeError):
    """The balance-history foundation cannot be installed safely."""


_KINDS = (
    ('POINT_AVAILABLE', 'point_accounts', 'account_id', 'available_micro'),
    ('POINT_LOCKED', 'point_accounts', 'account_id', 'locked_micro'),
    ('SHARE_AVAILABLE', 'outcome_positions', 'account_id, market_id, outcome', 'available_shares'),
    ('SHARE_LOCKED', 'outcome_positions', 'account_id, market_id, outcome', 'locked_shares'),
    ('MARKET_RESERVE', 'market_reserves', 'market_id', 'reserve_micro'),
)


def _json(value):
    return json.dumps(value, ensure_ascii=False, allow_nan=False, separators=(',', ':'))


def _checkpoint(conn):
    rows = []
    for kind, table, keys, value in _KINDS:
        for raw in conn.execute(f'SELECT {keys}, {value} FROM {table}'):
            raw = tuple(raw)
            if kind.startswith('POINT_'):
                account, market, outcome, amount = raw[0], None, None, raw[1]
            elif kind.startswith('SHARE_'):
                account, market, outcome, amount = raw
            else:
                account, market, outcome, amount = None, raw[0], None, raw[1]
            if (type(amount) is not int or not 0 <= amount <= 9223372036854775807
                    or any(not isinstance(x, str) or not x for x in (account, market, outcome) if x is not None)):
                raise HistoryError('invalid_bucket')
            rows.append([kind, account, market, outcome, amount])
    rows.sort(key=lambda r: _json(r[:4]).encode('utf-8'))
    return rows


def _digest(generation, rows):
    return hashlib.sha256(_json(['balance_checkpoint_v1', generation, rows]).encode('utf-8')).hexdigest()


def install_checkpoint(conn: sqlite3.Connection) -> None:
    """Install checkpoint and supported writer guards in the caller's transaction."""
    return _install_checkpoint_internal(conn, migration_fixture=None)


def _install_checkpoint_for_migration(fixture, *, failure_hook=None) -> None:
    """Restricted entry point for the active, owned fixture migration only."""
    from app.v2_clob_migration import _history_migration_authorized
    if not _history_migration_authorized(fixture):
        raise HistoryError('fixture_migration_required')
    return _install_checkpoint_internal(fixture.conn, migration_fixture=fixture, failure_hook=failure_hook)


def _install_checkpoint_internal(conn, migration_fixture, failure_hook=None) -> None:
    if not conn.in_transaction:
        raise HistoryError('transaction_required')
    if migration_fixture is None:
        if conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
            raise HistoryError('foreign_keys_required')
    else:
        from app.v2_clob_migration import _history_migration_authorized
        if migration_fixture.conn is not conn or not _history_migration_authorized(migration_fixture):
            raise HistoryError('fixture_migration_required')
    if conn.execute('PRAGMA user_version').fetchone()[0] != 2:
        raise HistoryError('v2_required')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'balance_history_%' LIMIT 1").fetchone():
        raise HistoryError('existing_history_requires_validation')
    from app.v2_clob_schema import validate_v2_schema
    from app.storage import verify_audit_chain
    from app.collateral_ledger import verify_collateral_invariants
    validate_v2_schema(conn)
    if conn.execute('PRAGMA integrity_check').fetchone()[0] != 'ok' or conn.execute('PRAGMA foreign_key_check').fetchone():
        raise HistoryError('database_integrity_invalid')
    if verify_audit_chain(conn)['integrity_status'] not in ('verified', 'empty'):
        raise HistoryError('audit_invalid')
    if verify_collateral_invariants(conn)['integrity_status'] != 'verified':
        raise HistoryError('invariants_invalid')
    rows = _checkpoint(conn)
    generation = uuid.uuid4().hex
    audit = conn.execute('SELECT id,event_hash FROM demo_audit_events ORDER BY id DESC LIMIT 1').fetchone()
    conn.execute('''CREATE TABLE balance_history_generation (
        generation_id TEXT PRIMARY KEY NOT NULL, format_version INTEGER NOT NULL CHECK(format_version=1),
        checkpoint_count INTEGER NOT NULL CHECK(checkpoint_count>=0), checkpoint_sha256 TEXT NOT NULL,
        point_available_count INTEGER NOT NULL CHECK(point_available_count>=0), point_locked_count INTEGER NOT NULL CHECK(point_locked_count>=0),
        share_available_count INTEGER NOT NULL CHECK(share_available_count>=0), share_locked_count INTEGER NOT NULL CHECK(share_locked_count>=0),
        market_reserve_count INTEGER NOT NULL CHECK(market_reserve_count>=0), audit_boundary_id INTEGER NOT NULL CHECK(typeof(audit_boundary_id)='integer' AND audit_boundary_id>=0), audit_boundary_hash TEXT, supply_watermark INTEGER NOT NULL CHECK(supply_watermark>=0), allocation_watermark INTEGER NOT NULL CHECK(allocation_watermark>=0), reserve_watermark INTEGER NOT NULL CHECK(reserve_watermark>=0), order_watermark INTEGER NOT NULL CHECK(order_watermark>=0), finalized INTEGER NOT NULL CHECK(finalized=1))''')
    # A context cannot survive COMMIT: its deferred parent is permanently empty.
    conn.execute('CREATE TABLE balance_history_context_must_be_empty (guard INTEGER PRIMARY KEY)')
    for action in ('INSERT', 'UPDATE', 'DELETE'):
        conn.execute(f'''CREATE TRIGGER balance_history_sentinel_{action.lower()}
                         BEFORE {action} ON balance_history_context_must_be_empty
                         BEGIN SELECT RAISE(ABORT, 'balance_history_sentinel_immutable'); END''')
    conn.execute('''CREATE TABLE balance_history_context (
        operation_id TEXT PRIMARY KEY NOT NULL CHECK(length(operation_id)>0),
        guard INTEGER NOT NULL CHECK(guard=1),
        account_id TEXT, source_id INTEGER, amount INTEGER, used INTEGER NOT NULL DEFAULT 0 CHECK(used IN (0,1,2,3,4)), source_type TEXT NOT NULL DEFAULT 'point_supply_event' CHECK(source_type IN ('point_supply_event','point_allocation_event','reserve_event','bucket_create','order_collateral_event')), destination_id TEXT, participant_id TEXT, market_id TEXT, quantity INTEGER, event_type TEXT, reservation_id INTEGER, outcome TEXT, asset_type TEXT,
        FOREIGN KEY(guard) REFERENCES balance_history_context_must_be_empty(guard)
            DEFERRABLE INITIALLY DEFERRED)''')
    conn.execute('''CREATE TABLE balance_history_operation (
        operation_id TEXT PRIMARY KEY, source_type TEXT NOT NULL CHECK(source_type IN ('point_supply_event','point_allocation_event','reserve_event','bucket_create','order_collateral_event')),
        source_id NOT NULL, account_id TEXT, market_id TEXT,
        supply_source INTEGER GENERATED ALWAYS AS (CASE WHEN source_type='point_supply_event' THEN source_id END) STORED REFERENCES point_supply_events(id),
        allocation_source INTEGER GENERATED ALWAYS AS (CASE WHEN source_type='point_allocation_event' THEN source_id END) STORED REFERENCES point_allocation_events(id),
        reserve_source INTEGER GENERATED ALWAYS AS (CASE WHEN source_type='reserve_event' THEN source_id END) STORED REFERENCES reserve_events(id),
        market_source TEXT GENERATED ALWAYS AS (CASE WHEN source_type='bucket_create' THEN source_id END) STORED REFERENCES collateral_markets(market_id),
        order_source INTEGER GENERATED ALWAYS AS (CASE WHEN source_type='order_collateral_event' THEN source_id END) STORED REFERENCES order_collateral_events(id),
        amount INTEGER NOT NULL CHECK(typeof(amount)='integer' AND amount>=0),
        payload_hash TEXT NOT NULL, UNIQUE(source_type,source_id))''')
    conn.execute('''CREATE TABLE balance_history_bucket (
        bucket_id INTEGER PRIMARY KEY, generation_id TEXT NOT NULL REFERENCES balance_history_generation(generation_id),
        kind TEXT NOT NULL, account_id TEXT, market_id TEXT, outcome TEXT,
        opening_value INTEGER NOT NULL CHECK(typeof(opening_value)='integer' AND opening_value>=0),
        origin TEXT NOT NULL CHECK(origin IN ('checkpoint','created')), creation_operation_id TEXT REFERENCES balance_history_operation(operation_id) DEFERRABLE INITIALLY DEFERRED,
        CHECK((origin='checkpoint' AND creation_operation_id IS NULL) OR (origin='created' AND opening_value=0 AND creation_operation_id IS NOT NULL)),
        CHECK ((kind IN ('POINT_AVAILABLE','POINT_LOCKED') AND account_id IS NOT NULL AND market_id IS NULL AND outcome IS NULL)
          OR (kind IN ('SHARE_AVAILABLE','SHARE_LOCKED') AND account_id IS NOT NULL AND market_id IS NOT NULL AND outcome IN ('YES','NO'))
          OR (kind='MARKET_RESERVE' AND account_id IS NULL AND market_id IS NOT NULL AND outcome IS NULL)))''')
    for name, fields, cond in (
        ('point', 'generation_id,kind,account_id', "kind IN ('POINT_AVAILABLE','POINT_LOCKED')"),
        ('share', 'generation_id,kind,account_id,market_id,outcome', "kind IN ('SHARE_AVAILABLE','SHARE_LOCKED')"),
        ('reserve', 'generation_id,kind,market_id', "kind='MARKET_RESERVE'")):
        conn.execute(f'CREATE UNIQUE INDEX balance_history_bucket_{name} ON balance_history_bucket({fields}) WHERE {cond}')
    counts=[sum(row[0]==kind for row in rows) for kind in ('POINT_AVAILABLE','POINT_LOCKED','SHARE_AVAILABLE','SHARE_LOCKED','MARKET_RESERVE')]
    conn.execute('INSERT INTO balance_history_generation VALUES (?,1,?,?,?,?,?,?,?,?,?,?,?,?,?,1)', (generation, len(rows), _digest(generation, rows), *counts, audit[0] if audit else 0, audit[1] if audit else None, conn.execute('SELECT coalesce(max(id),0) FROM point_supply_events').fetchone()[0], conn.execute('SELECT coalesce(max(id),0) FROM point_allocation_events').fetchone()[0], conn.execute('SELECT coalesce(max(id),0) FROM reserve_events').fetchone()[0], conn.execute('SELECT coalesce(max(id),0) FROM order_collateral_events').fetchone()[0]))
    conn.executemany('''INSERT INTO balance_history_bucket(generation_id,kind,account_id,market_id,outcome,opening_value,origin)
                        VALUES (?,?,?,?,?,?, 'checkpoint')''', [(generation, *row) for row in rows])
    if failure_hook is not None:
        failure_hook('history_partial')
    _install_bootstrap_protocol(conn)
    _install_allocation_protocol(conn)
    _install_market_protocol(conn)
    _install_order_protocol(conn)



def validate_bootstrap_source(conn: sqlite3.Connection, *, source_id: int,
                              account_id: str, before: int, after: int) -> None:
    """Check a bootstrap source and its existing ledger evidence, without writing.

    This check alone does not authorize a balance change. In particular it
    does not establish that the source is after the checkpoint, single-use,
    or associated with a finalized history operation.
    """
    from app.storage import verify_audit_chain

    if (type(source_id) is not int or source_id <= 0 or not isinstance(account_id, str)
            or not account_id or type(before) is not int or type(after) is not int
            or not 0 <= before < after <= 9223372036854775807):
        raise HistoryError('invalid_bootstrap_source')
    amount = after - before
    source = conn.execute('''SELECT engine_key,event_type,destination_account_id,amount_micro
        FROM point_supply_events WHERE id=?''', (source_id,)).fetchone()
    if source is None or tuple(source) != ('collateralized_clob_v2', 'bootstrap_issue', account_id, amount):
        raise HistoryError('invalid_bootstrap_source')
    owner = conn.execute('SELECT engine_key,owner_type FROM point_accounts WHERE account_id=?',
                         (account_id,)).fetchone()
    if owner is None or tuple(owner) != ('collateralized_clob_v2', 'operator'):
        raise HistoryError('invalid_bootstrap_source')
    rows = conn.execute('''SELECT engine_key,account_id,market_id,entry_type,amount_micro,
        account_available_before_micro,account_available_after_micro,reserve_before_micro,reserve_after_micro
        FROM collateral_ledger_entries WHERE reference_type='point_supply_event' AND reference_id=?''',
        (str(source_id),)).fetchall()
    expected = ('collateralized_clob_v2', account_id, None, 'bootstrap_issue', amount,
                before, after, None, None)
    if len(rows) != 1 or tuple(rows[0]) != expected:
        raise HistoryError('invalid_bootstrap_ledger')
    audit = conn.execute('''SELECT count(*) FROM demo_audit_events
        WHERE event_type='v2_point_supply_bootstrapped' AND reference_type='point_supply_event'
          AND reference_id=?''', (str(source_id),)).fetchone()[0]
    if audit != 1 or verify_audit_chain(conn)['integrity_status'] != 'verified':
        raise HistoryError('invalid_bootstrap_audit')


def _install_bootstrap_protocol(conn):
    conn.execute('''CREATE TABLE balance_history_event (
        event_seq INTEGER PRIMARY KEY AUTOINCREMENT,
        generation_id TEXT NOT NULL REFERENCES balance_history_generation(generation_id),
        bucket_id INTEGER NOT NULL REFERENCES balance_history_bucket(bucket_id),
        operation_id TEXT NOT NULL REFERENCES balance_history_operation(operation_id) DEFERRABLE INITIALLY DEFERRED,
        role TEXT NOT NULL CHECK(role IN ('BOOTSTRAP_CREDIT','ALLOCATION_DEBIT','ALLOCATION_CREDIT','SPLIT_POINT','SPLIT_RESERVE','SPLIT_YES','SPLIT_NO','MERGE_POINT','MERGE_RESERVE','MERGE_YES','MERGE_NO','ORDER_AVAILABLE','ORDER_LOCKED')),
        balance_before INTEGER NOT NULL CHECK(typeof(balance_before)='integer' AND balance_before>=0),
        delta INTEGER NOT NULL CHECK(typeof(delta)='integer' AND delta<>0),
        balance_after INTEGER NOT NULL CHECK(typeof(balance_after)='integer' AND balance_after>=0 AND balance_after-balance_before=delta),
        UNIQUE(operation_id,bucket_id,role))''')
    conn.execute('CREATE INDEX balance_history_event_bucket ON balance_history_event(generation_id,bucket_id,event_seq)')
    conn.execute('CREATE INDEX balance_history_event_operation ON balance_history_event(operation_id,role)')
    conn.execute('''CREATE TRIGGER balance_history_context_fk_guard BEFORE INSERT ON balance_history_context
      WHEN (SELECT foreign_keys FROM pragma_foreign_keys)!=1
      BEGIN SELECT RAISE(ABORT,'balance_history_foreign_keys_required'); END''')
    conn.execute('''CREATE TRIGGER balance_history_context_single BEFORE INSERT ON balance_history_context
      WHEN EXISTS(SELECT 1 FROM balance_history_context)
      BEGIN SELECT RAISE(ABORT,'balance_history_context_exists'); END''')
    conn.execute('''CREATE TRIGGER balance_history_event_insert_guard BEFORE INSERT ON balance_history_event WHEN NEW.role='BOOTSTRAP_CREDIT' BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c
       JOIN balance_history_bucket b ON b.bucket_id=NEW.bucket_id
       JOIN point_accounts a ON a.account_id=b.account_id
       JOIN point_supply_events s ON s.id=c.source_id
       WHERE c.operation_id=NEW.operation_id AND c.used=0 AND c.account_id=b.account_id
       AND b.kind='POINT_AVAILABLE' AND NEW.generation_id=b.generation_id
       AND NEW.role='BOOTSTRAP_CREDIT' AND NEW.delta=c.amount AND s.amount_micro=c.amount
       AND s.destination_account_id=c.account_id AND s.engine_key='collateralized_clob_v2'
       AND s.event_type='bootstrap_issue' AND s.id>(SELECT supply_watermark FROM balance_history_generation)
       AND NEW.balance_before=coalesce((SELECT e.balance_after FROM balance_history_event e
            WHERE e.bucket_id=b.bucket_id ORDER BY e.event_seq DESC LIMIT 1),b.opening_value)
       AND NEW.balance_after=a.available_micro)
      THEN RAISE(ABORT,'balance_history_event_invalid') END; END''')
    for table, condition in (
        ('point_supply_events', "EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type='point_supply_event' AND o.source_id=OLD.id)"),
        ('collateral_ledger_entries', "OLD.reference_type='point_supply_event' AND EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type=OLD.reference_type AND CAST(o.source_id AS TEXT)=OLD.reference_id)"),
        ('demo_audit_events', "OLD.reference_type='point_supply_event' AND EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type=OLD.reference_type AND CAST(o.source_id AS TEXT)=OLD.reference_id)"),
    ):
        for action in ('UPDATE', 'DELETE'):
            conn.execute(f'''CREATE TRIGGER balance_history_{table}_final_{action.lower()} BEFORE {action} ON {table}
                WHEN {condition} BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    conn.execute('''CREATE TRIGGER balance_history_context_delete_guard BEFORE DELETE ON balance_history_context
      WHEN NOT EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.operation_id=OLD.operation_id)
      BEGIN SELECT RAISE(ABORT,'balance_history_operation_unfinalized'); END''')
    conn.execute('''CREATE TRIGGER balance_history_supply_insert_guard BEFORE INSERT ON point_supply_events BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c WHERE c.used=0
       AND c.account_id=NEW.destination_account_id AND c.amount=NEW.amount_micro AND c.source_id IS NULL
       AND NEW.engine_key='collateralized_clob_v2' AND NEW.event_type='bootstrap_issue')
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_supply_after_insert AFTER INSERT ON point_supply_events BEGIN
      UPDATE balance_history_context SET source_id=NEW.id WHERE source_type='point_supply_event' AND account_id=NEW.destination_account_id AND source_id IS NULL AND used=0;
      SELECT CASE WHEN NEW.id <= (SELECT supply_watermark FROM balance_history_generation)
      THEN RAISE(ABORT,'balance_history_source_invalid') END; END''')
    for table in ('collateral_ledger_entries', 'demo_audit_events'):
        conn.execute(f'''CREATE TRIGGER balance_history_{table}_final_insert BEFORE INSERT ON {table}
          WHEN NEW.reference_type='point_supply_event' AND EXISTS(
            SELECT 1 FROM balance_history_operation o WHERE o.source_type=NEW.reference_type AND CAST(o.source_id AS TEXT)=NEW.reference_id)
          BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    # An absent or incomplete context never authorizes an account creation.
    conn.execute('''CREATE TRIGGER balance_history_point_insert_guard BEFORE INSERT ON point_accounts
      WHEN NOT EXISTS(SELECT 1 FROM point_accounts WHERE account_id=NEW.account_id) AND NOT EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='point_allocation_event')
      BEGIN SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_supply_events s ON s.id=c.source_id
       WHERE c.account_id=NEW.account_id AND c.used=0 AND s.destination_account_id=NEW.account_id
       AND s.engine_key='collateralized_clob_v2' AND s.event_type='bootstrap_issue' AND s.amount_micro=c.amount
       AND s.id>(SELECT supply_watermark FROM balance_history_generation) AND NEW.engine_key=s.engine_key
       AND NEW.owner_type='operator' AND NEW.owner_id='operator-treasury'
       AND NEW.available_micro=0 AND NEW.locked_micro=0)
       THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_point_insert AFTER INSERT ON point_accounts BEGIN
      INSERT INTO balance_history_bucket(generation_id,kind,account_id,opening_value,origin,creation_operation_id)
      SELECT g.generation_id,'POINT_AVAILABLE',NEW.account_id,0,'created',c.operation_id FROM balance_history_generation g,balance_history_context c WHERE c.account_id=NEW.account_id OR (c.source_type='point_allocation_event' AND c.destination_id=NEW.account_id);
      INSERT INTO balance_history_bucket(generation_id,kind,account_id,opening_value,origin,creation_operation_id)
      SELECT g.generation_id,'POINT_LOCKED',NEW.account_id,0,'created',c.operation_id FROM balance_history_generation g,balance_history_context c WHERE c.account_id=NEW.account_id OR (c.source_type='point_allocation_event' AND c.destination_id=NEW.account_id);
    END''')
    conn.execute('''CREATE TRIGGER balance_history_point_update_guard BEFORE UPDATE ON point_accounts WHEN NOT EXISTS(SELECT 1 FROM balance_history_context WHERE source_type IN ('point_allocation_event','reserve_event','order_collateral_event')) BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_supply_events s ON s.id=c.source_id
       JOIN balance_history_bucket b ON b.account_id=OLD.account_id AND b.kind='POINT_AVAILABLE'
       WHERE c.account_id=OLD.account_id AND c.used=0 AND c.amount>0 AND s.amount_micro=c.amount
       AND s.destination_account_id=OLD.account_id AND s.event_type='bootstrap_issue' AND s.engine_key='collateralized_clob_v2'
       AND s.id>(SELECT supply_watermark FROM balance_history_generation)
       AND OLD.engine_key=s.engine_key AND NEW.engine_key=OLD.engine_key AND NEW.account_id=OLD.account_id
       AND NEW.owner_id=OLD.owner_id AND NEW.owner_type=OLD.owner_type AND NEW.locked_micro=OLD.locked_micro
       AND typeof(NEW.available_micro)='integer' AND NEW.available_micro>OLD.available_micro
       AND NEW.available_micro-OLD.available_micro=c.amount
       AND OLD.available_micro=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=b.bucket_id ORDER BY e.event_seq DESC LIMIT 1),b.opening_value))
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_point_update AFTER UPDATE ON point_accounts WHEN NOT EXISTS(SELECT 1 FROM balance_history_context WHERE source_type IN ('point_allocation_event','reserve_event','order_collateral_event')) BEGIN
      INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after)
      SELECT b.generation_id,b.bucket_id,c.operation_id,'BOOTSTRAP_CREDIT',OLD.available_micro,c.amount,NEW.available_micro
      FROM balance_history_bucket b JOIN balance_history_context c ON c.account_id=OLD.account_id
      WHERE b.kind='POINT_AVAILABLE' AND b.account_id=OLD.account_id;
      UPDATE balance_history_context SET used=1 WHERE account_id=OLD.account_id AND used=0;
    END''')
    for table in ('outcome_positions','market_reserves'):
        for action in ('DELETE',):
            conn.execute(f'''CREATE TRIGGER balance_history_guard_{table}_{action.lower()} BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'balance_history_source_protocol_unavailable'); END''')
    conn.execute('''CREATE TRIGGER balance_history_point_delete BEFORE DELETE ON point_accounts
      BEGIN SELECT RAISE(ABORT,'balance_history_source_protocol_unavailable'); END''')
    conn.execute('''CREATE TRIGGER balance_history_bucket_insert_guard BEFORE INSERT ON balance_history_bucket
      WHEN NEW.origin!='created' OR NOT EXISTS(SELECT 1 FROM balance_history_context c WHERE c.operation_id=NEW.creation_operation_id AND ((NEW.kind IN ('POINT_AVAILABLE','POINT_LOCKED') AND (c.account_id=NEW.account_id OR (c.source_type='point_allocation_event' AND c.destination_id=NEW.account_id)) AND c.used=0) OR (NEW.kind='MARKET_RESERVE' AND c.source_type='bucket_create' AND c.market_id=NEW.market_id AND c.used=0) OR (NEW.kind IN ('SHARE_AVAILABLE','SHARE_LOCKED') AND c.source_type='reserve_event' AND c.account_id=NEW.account_id AND c.market_id=NEW.market_id AND ((c.used=2 AND NEW.outcome='YES') OR (c.used=3 AND NEW.outcome='NO')))))
      BEGIN SELECT RAISE(ABORT,'balance_history_immutable'); END''')
    for table in ('balance_history_generation','balance_history_bucket','balance_history_operation','balance_history_event'):
        for action in ('UPDATE','DELETE'):
            conn.execute(f'''CREATE TRIGGER {table}_immutable_{action.lower()} BEFORE {action} ON {table}
                BEGIN SELECT RAISE(ABORT,'balance_history_immutable'); END''')
    conn.execute('''CREATE TRIGGER balance_history_generation_no_insert BEFORE INSERT ON balance_history_generation
      BEGIN SELECT RAISE(ABORT,'balance_history_immutable'); END''')
    # Even direct SQL cannot finalize until the source, ledger, audit, event and live balance agree.
    conn.execute('''CREATE TRIGGER balance_history_operation_validate BEFORE INSERT ON balance_history_operation WHEN NEW.source_type='point_supply_event' BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c
       JOIN point_supply_events s ON s.id=c.source_id JOIN balance_history_event e ON e.operation_id=c.operation_id
       JOIN balance_history_bucket b ON b.bucket_id=e.bucket_id JOIN point_accounts a ON a.account_id=c.account_id
       JOIN collateral_ledger_entries l ON l.reference_type='point_supply_event' AND l.reference_id=cast(s.id AS TEXT)
       WHERE c.operation_id=NEW.operation_id AND c.used=1 AND c.account_id=NEW.account_id AND c.amount=NEW.amount
       AND NEW.source_id=s.id AND NEW.payload_hash=s.payload_hash AND s.engine_key='collateralized_clob_v2'
       AND s.event_type='bootstrap_issue' AND s.destination_account_id=NEW.account_id AND s.amount_micro=NEW.amount
       AND s.id>(SELECT supply_watermark FROM balance_history_generation)
       AND b.kind='POINT_AVAILABLE' AND b.account_id=NEW.account_id AND e.delta=NEW.amount AND a.available_micro=e.balance_after
       AND l.engine_key=s.engine_key AND l.account_id=NEW.account_id AND l.entry_type='bootstrap_issue' AND l.amount_micro=NEW.amount
       AND l.account_available_before_micro=e.balance_before AND l.account_available_after_micro=e.balance_after
       AND (SELECT count(*) FROM balance_history_event WHERE operation_id=NEW.operation_id)=1
       AND (SELECT count(*) FROM collateral_ledger_entries WHERE reference_type='point_supply_event' AND reference_id=cast(s.id AS TEXT))=1
       AND (SELECT count(*) FROM demo_audit_events WHERE event_type='v2_point_supply_bootstrapped' AND reference_type='point_supply_event' AND reference_id=cast(s.id AS TEXT))=1)
      THEN RAISE(ABORT,'balance_history_manifest_invalid') END; END''')


def begin_bootstrap(conn, *, source_id, account_id, amount):
    if not conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise HistoryError('history_transaction_required')
    op = uuid.uuid4().hex
    conn.execute('INSERT INTO balance_history_context(operation_id,guard,account_id,source_id,amount) VALUES (?,1,?,?,?)',
                 (op, account_id, source_id, amount))
    return op


def finalize_bootstrap(conn, *, operation_id, source_id, account_id, before, after, payload_hash):
    validate_bootstrap_source(conn, source_id=source_id, account_id=account_id, before=before, after=after)
    conn.execute('''INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,amount,payload_hash)
                    VALUES (?,'point_supply_event',?,?,?,?)''',
                 (operation_id, source_id, account_id, after-before, payload_hash))
    conn.execute('DELETE FROM balance_history_context WHERE operation_id=?', (operation_id,))


def _install_allocation_protocol(conn):
    conn.execute('''CREATE TRIGGER balance_history_allocation_account BEFORE INSERT ON point_accounts
      WHEN NOT EXISTS(SELECT 1 FROM point_accounts WHERE account_id=NEW.account_id)
       AND EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='point_allocation_event') BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_accounts treasury ON treasury.account_id=c.account_id
       WHERE c.source_type='point_allocation_event' AND c.used=0 AND c.source_id IS NULL
       AND c.destination_id=NEW.account_id AND c.participant_id=NEW.owner_id
       AND NEW.engine_key='collateralized_clob_v2' AND NEW.owner_type='participant'
       AND NEW.account_id='collateralized_clob_v2:participant:'||c.participant_id
       AND NEW.available_micro=0 AND NEW.locked_micro=0
       AND treasury.engine_key=NEW.engine_key AND treasury.owner_type='operator' AND treasury.owner_id='operator-treasury'
       AND (EXISTS(SELECT 1 FROM demo_users WHERE user_id=c.participant_id) OR EXISTS(SELECT 1 FROM user_accounts WHERE participant_id=c.participant_id AND account_status='active')))
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_allocation_source BEFORE INSERT ON point_allocation_events BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_accounts a ON a.account_id=c.account_id
       JOIN point_accounts b ON b.account_id=c.destination_id WHERE c.source_type='point_allocation_event' AND c.used=0 AND c.source_id IS NULL
       AND NEW.engine_key='collateralized_clob_v2' AND NEW.source_account_id=c.account_id
       AND NEW.destination_account_id=c.destination_id AND NEW.participant_id=c.participant_id AND NEW.amount_micro=c.amount
       AND a.available_micro=NEW.source_before_micro AND b.available_micro=NEW.destination_before_micro
       AND a.owner_type='operator' AND a.owner_id='operator-treasury' AND b.owner_type='participant' AND b.owner_id=c.participant_id
       AND a.engine_key=NEW.engine_key AND b.engine_key=NEW.engine_key)
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_allocation_source_after AFTER INSERT ON point_allocation_events BEGIN
      UPDATE balance_history_context SET source_id=NEW.id WHERE source_type='point_allocation_event' AND source_id IS NULL AND used=0;
      SELECT CASE WHEN NEW.id <= (SELECT allocation_watermark FROM balance_history_generation) THEN RAISE(ABORT,'balance_history_source_invalid') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_allocation_update BEFORE UPDATE ON point_accounts
      WHEN EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='point_allocation_event') BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_allocation_events s ON s.id=c.source_id
       JOIN balance_history_bucket b ON b.account_id=OLD.account_id AND b.kind='POINT_AVAILABLE'
       WHERE c.source_type='point_allocation_event' AND s.id>(SELECT allocation_watermark FROM balance_history_generation)
       AND s.engine_key=OLD.engine_key AND NEW.engine_key=OLD.engine_key AND NEW.account_id=OLD.account_id
       AND NEW.owner_id=OLD.owner_id AND NEW.owner_type=OLD.owner_type AND NEW.locked_micro=OLD.locked_micro
       AND typeof(NEW.available_micro)='integer' AND NEW.available_micro>=0
       AND OLD.available_micro=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=b.bucket_id ORDER BY e.event_seq DESC LIMIT 1),b.opening_value)
       AND ((c.used=0 AND OLD.account_id=c.account_id AND OLD.available_micro=s.source_before_micro AND NEW.available_micro=s.source_after_micro)
         OR (c.used=1 AND OLD.account_id=c.destination_id AND OLD.available_micro=s.destination_before_micro AND NEW.available_micro=s.destination_after_micro)))
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_allocation_update_after AFTER UPDATE ON point_accounts
      WHEN EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='point_allocation_event') BEGIN
      INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after)
      SELECT b.generation_id,b.bucket_id,c.operation_id,CASE WHEN c.used=0 THEN 'ALLOCATION_DEBIT' ELSE 'ALLOCATION_CREDIT' END,
       OLD.available_micro,NEW.available_micro-OLD.available_micro,NEW.available_micro
      FROM balance_history_bucket b JOIN balance_history_context c ON c.source_type='point_allocation_event'
      WHERE b.kind='POINT_AVAILABLE' AND b.account_id=OLD.account_id;
      UPDATE balance_history_context SET used=used+1 WHERE source_type='point_allocation_event'; END''')
    conn.execute('''CREATE TRIGGER balance_history_allocation_event BEFORE INSERT ON balance_history_event
      WHEN NEW.role IN ('ALLOCATION_DEBIT','ALLOCATION_CREDIT') BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_allocation_events s ON s.id=c.source_id
       JOIN balance_history_bucket b ON b.bucket_id=NEW.bucket_id JOIN point_accounts a ON a.account_id=b.account_id
       WHERE c.source_type='point_allocation_event' AND c.operation_id=NEW.operation_id AND b.kind='POINT_AVAILABLE'
       AND b.generation_id=NEW.generation_id AND a.available_micro=NEW.balance_after
       AND NEW.balance_before=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=b.bucket_id ORDER BY event_seq DESC LIMIT 1),b.opening_value)
       AND ((c.used=0 AND NEW.role='ALLOCATION_DEBIT' AND b.account_id=c.account_id AND NEW.delta=-c.amount
         AND NEW.balance_before=s.source_before_micro AND NEW.balance_after=s.source_after_micro)
        OR (c.used=1 AND NEW.role='ALLOCATION_CREDIT' AND b.account_id=c.destination_id AND NEW.delta=c.amount
         AND NEW.balance_before=s.destination_before_micro AND NEW.balance_after=s.destination_after_micro)))
      THEN RAISE(ABORT,'balance_history_event_invalid') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_allocation_final BEFORE INSERT ON balance_history_operation
      WHEN NEW.source_type='point_allocation_event' BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN point_allocation_events s ON s.id=c.source_id
        JOIN balance_history_event d ON d.operation_id=c.operation_id AND d.role='ALLOCATION_DEBIT'
        JOIN balance_history_event r ON r.operation_id=c.operation_id AND r.role='ALLOCATION_CREDIT'
        JOIN balance_history_bucket bd ON bd.bucket_id=d.bucket_id JOIN balance_history_bucket br ON br.bucket_id=r.bucket_id
        JOIN point_accounts a ON a.account_id=c.account_id JOIN point_accounts b ON b.account_id=c.destination_id
        JOIN collateral_ledger_entries ld ON ld.reference_type='point_allocation_event' AND ld.reference_id=cast(s.id AS TEXT) AND ld.entry_type='participant_allocation_debit'
        JOIN collateral_ledger_entries lr ON lr.reference_type='point_allocation_event' AND lr.reference_id=cast(s.id AS TEXT) AND lr.entry_type='participant_allocation_credit'
        WHERE c.operation_id=NEW.operation_id AND c.source_type=NEW.source_type AND c.used=2 AND NEW.source_id=s.id
        AND NEW.account_id=c.account_id AND NEW.amount=c.amount AND NEW.payload_hash=s.payload_hash
        AND s.source_account_id=c.account_id AND s.destination_account_id=c.destination_id AND s.participant_id=c.participant_id AND s.amount_micro=c.amount
        AND bd.account_id=c.account_id AND br.account_id=c.destination_id AND d.delta=-c.amount AND r.delta=c.amount
        AND a.available_micro=d.balance_after AND b.available_micro=r.balance_after
        AND ld.engine_key=s.engine_key AND ld.account_id=c.account_id AND ld.amount_micro=d.delta AND ld.account_available_before_micro=d.balance_before AND ld.account_available_after_micro=d.balance_after
        AND ld.market_id IS NULL AND ld.reserve_before_micro IS NULL AND ld.reserve_after_micro IS NULL
        AND lr.engine_key=s.engine_key AND lr.account_id=c.destination_id AND lr.amount_micro=r.delta AND lr.account_available_before_micro=r.balance_before AND lr.account_available_after_micro=r.balance_after
        AND lr.market_id IS NULL AND lr.reserve_before_micro IS NULL AND lr.reserve_after_micro IS NULL
        AND (SELECT count(*) FROM balance_history_event WHERE operation_id=c.operation_id)=2
        AND (SELECT count(*) FROM collateral_ledger_entries WHERE reference_type='point_allocation_event' AND reference_id=cast(s.id AS TEXT))=2
        AND (SELECT count(*) FROM demo_audit_events WHERE event_type='v2_participant_points_allocated' AND reference_type='point_allocation_event' AND reference_id=cast(s.id AS TEXT) AND user_id=c.participant_id)=1)
       THEN RAISE(ABORT,'balance_history_manifest_invalid') END; END''')
    for action in ('UPDATE','DELETE'):
        conn.execute(f'''CREATE TRIGGER balance_history_allocation_source_final_{action.lower()} BEFORE {action} ON point_allocation_events
          WHEN EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type='point_allocation_event' AND o.source_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    for table in ('collateral_ledger_entries','demo_audit_events'):
        for action in ('INSERT','UPDATE','DELETE'):
            prefix='NEW' if action=='INSERT' else 'OLD'
            conn.execute(f'''CREATE TRIGGER balance_history_allocation_{table}_{action.lower()} BEFORE {action} ON {table}
              WHEN {prefix}.reference_type='point_allocation_event' AND EXISTS(SELECT 1 FROM balance_history_operation o
                WHERE o.source_type='point_allocation_event' AND CAST(o.source_id AS TEXT)={prefix}.reference_id)
              BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')


def begin_allocation(conn, *, account_id, destination_id, participant_id, amount):
    if not conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone()[0] != 1:
        raise HistoryError('history_transaction_required')
    op=uuid.uuid4().hex
    conn.execute('''INSERT INTO balance_history_context(operation_id,guard,account_id,source_type,destination_id,participant_id,amount)
                 VALUES (?,1,?,'point_allocation_event',?,?,?)''', (op,account_id,destination_id,participant_id,amount))
    return op


def finalize_allocation(conn, *, operation_id, source_id, account_id, amount, payload_hash):
    from app.storage import verify_audit_chain
    if verify_audit_chain(conn)['integrity_status'] != 'verified':
        raise HistoryError('invalid_allocation_audit')
    conn.execute('''INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,amount,payload_hash)
                 VALUES (?,'point_allocation_event',?,?,?,?)''', (operation_id,source_id,account_id,amount,payload_hash))
    conn.execute('DELETE FROM balance_history_context WHERE operation_id=?',(operation_id,))


def _install_market_protocol(conn):
    conn.execute('''CREATE TRIGGER balance_history_market_insert BEFORE INSERT ON market_reserves BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN collateral_markets m ON m.market_id=c.market_id
       WHERE c.source_type='bucket_create' AND c.used=0 AND c.market_id=NEW.market_id
       AND m.engine_key='collateralized_clob_v2' AND m.status='open' AND NEW.reserve_micro=0 AND NEW.net_complete_sets=0)
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_market_insert_after AFTER INSERT ON market_reserves BEGIN
      INSERT INTO balance_history_bucket(generation_id,kind,market_id,opening_value,origin,creation_operation_id)
      SELECT g.generation_id,'MARKET_RESERVE',NEW.market_id,0,'created',c.operation_id FROM balance_history_generation g,balance_history_context c
      WHERE c.source_type='bucket_create' AND c.market_id=NEW.market_id AND c.used=0;
      UPDATE balance_history_context SET used=1 WHERE source_type='bucket_create' AND market_id=NEW.market_id AND used=0; END''')
    conn.execute('''CREATE TRIGGER balance_history_position_insert BEFORE INSERT ON outcome_positions BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN reserve_events s ON s.id=c.source_id
       WHERE c.source_type='reserve_event' AND c.event_type='split' AND s.event_type='split' AND c.account_id=NEW.account_id AND c.market_id=NEW.market_id
       AND ((c.used=2 AND NEW.outcome='YES') OR (c.used=3 AND NEW.outcome='NO')) AND NEW.available_shares=0 AND NEW.locked_shares=0)
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_position_insert_after AFTER INSERT ON outcome_positions BEGIN
      INSERT INTO balance_history_bucket(generation_id,kind,account_id,market_id,outcome,opening_value,origin,creation_operation_id)
      SELECT g.generation_id,'SHARE_AVAILABLE',NEW.account_id,NEW.market_id,NEW.outcome,0,'created',c.operation_id FROM balance_history_generation g,balance_history_context c
      WHERE c.source_type='reserve_event' AND c.account_id=NEW.account_id AND c.market_id=NEW.market_id;
      INSERT INTO balance_history_bucket(generation_id,kind,account_id,market_id,outcome,opening_value,origin,creation_operation_id)
      SELECT g.generation_id,'SHARE_LOCKED',NEW.account_id,NEW.market_id,NEW.outcome,0,'created',c.operation_id FROM balance_history_generation g,balance_history_context c
      WHERE c.source_type='reserve_event' AND c.account_id=NEW.account_id AND c.market_id=NEW.market_id; END''')
    conn.execute('''CREATE TRIGGER balance_history_reserve_source BEFORE INSERT ON reserve_events BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN market_reserves r ON r.market_id=c.market_id
       JOIN point_accounts a ON a.account_id=c.account_id JOIN collateral_markets m ON m.market_id=c.market_id
       WHERE c.source_type='reserve_event' AND c.used=0 AND c.source_id IS NULL AND NEW.engine_key='collateralized_clob_v2'
       AND NEW.account_id=c.account_id AND NEW.market_id=c.market_id AND NEW.event_type=c.event_type AND NEW.quantity=c.quantity
       AND NEW.points_micro=c.amount AND NEW.points_micro=NEW.quantity*10000 AND typeof(NEW.points_micro)='integer'
       AND NEW.reserve_before_micro=r.reserve_micro AND a.engine_key=NEW.engine_key AND m.engine_key=NEW.engine_key AND m.status='open'
       AND ((NEW.event_type='split' AND NEW.reserve_after_micro-NEW.reserve_before_micro=NEW.points_micro)
         OR (NEW.event_type='merge' AND NEW.reserve_after_micro-NEW.reserve_before_micro=-NEW.points_micro)))
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_reserve_source_after AFTER INSERT ON reserve_events BEGIN
      UPDATE balance_history_context SET source_id=NEW.id WHERE source_type='reserve_event' AND source_id IS NULL AND used=0;
      SELECT CASE WHEN NEW.id<=(SELECT reserve_watermark FROM balance_history_generation) THEN RAISE(ABORT,'balance_history_source_invalid') END; END''')
    # Each step authorizes exactly one key; event trigger records the step before incrementing it.
    specs=(
      ('point_accounts',"c.used=0 AND OLD.account_id=c.account_id AND NEW.account_id=OLD.account_id AND NEW.engine_key=OLD.engine_key AND NEW.owner_id=OLD.owner_id AND NEW.owner_type=OLD.owner_type AND NEW.locked_micro=OLD.locked_micro",'POINT_AVAILABLE',"b.account_id=OLD.account_id",'available_micro',"CASE WHEN s.event_type='split' THEN -s.points_micro ELSE s.points_micro END",'POINT'),
      ('market_reserves',"c.used=1 AND OLD.market_id=c.market_id AND NEW.market_id=OLD.market_id AND NEW.net_complete_sets-OLD.net_complete_sets=CASE WHEN s.event_type='split' THEN s.quantity ELSE -s.quantity END",'MARKET_RESERVE',"b.market_id=OLD.market_id",'reserve_micro',"CASE WHEN s.event_type='split' THEN s.points_micro ELSE -s.points_micro END",'RESERVE'),
      ('outcome_positions',"((c.used=2 AND OLD.outcome='YES') OR (c.used=3 AND OLD.outcome='NO')) AND OLD.account_id=c.account_id AND OLD.market_id=c.market_id AND NEW.account_id=OLD.account_id AND NEW.market_id=OLD.market_id AND NEW.outcome=OLD.outcome AND NEW.locked_shares=OLD.locked_shares",'SHARE_AVAILABLE',"b.account_id=OLD.account_id AND b.market_id=OLD.market_id AND b.outcome=OLD.outcome",'available_shares',"CASE WHEN s.event_type='split' THEN s.quantity ELSE -s.quantity END",None),
    )
    for table,identity,kind,bucket,field,delta,label in specs:
        when="WHEN EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='reserve_event')" if table=='point_accounts' else ("WHEN NOT EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='order_collateral_event')" if table=='outcome_positions' else '')
        role=f"'{label}'" if label else 'OLD.outcome'
        conn.execute(f'''CREATE TRIGGER balance_history_market_{table}_update BEFORE UPDATE ON {table} {when} BEGIN
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN reserve_events s ON s.id=c.source_id
           JOIN balance_history_bucket b ON b.kind='{kind}' AND {bucket}
           WHERE c.source_type='reserve_event' AND s.id>(SELECT reserve_watermark FROM balance_history_generation)
           AND s.account_id=c.account_id AND s.market_id=c.market_id AND s.event_type=c.event_type AND s.quantity=c.quantity
           AND {identity} AND typeof(NEW.{field})='integer' AND NEW.{field}>=0 AND NEW.{field}-OLD.{field}=({delta})
           AND OLD.{field}=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=b.bucket_id ORDER BY e.event_seq DESC LIMIT 1),b.opening_value))
          THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
        conn.execute(f'''CREATE TRIGGER balance_history_market_{table}_update_after AFTER UPDATE ON {table} {when} BEGIN
          INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after)
          SELECT b.generation_id,b.bucket_id,c.operation_id,upper(s.event_type)||'_'||{role},OLD.{field},NEW.{field}-OLD.{field},NEW.{field}
          FROM balance_history_bucket b JOIN balance_history_context c ON c.source_type='reserve_event' JOIN reserve_events s ON s.id=c.source_id
          WHERE b.kind='{kind}' AND {bucket};
          UPDATE balance_history_context SET used=used+1 WHERE source_type='reserve_event'; END''')
    conn.execute('''CREATE TRIGGER balance_history_reserve_event_guard BEFORE INSERT ON balance_history_event
      WHEN NEW.role IN ('SPLIT_POINT','SPLIT_RESERVE','SPLIT_YES','SPLIT_NO','MERGE_POINT','MERGE_RESERVE','MERGE_YES','MERGE_NO') BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN reserve_events s ON s.id=c.source_id JOIN balance_history_bucket b ON b.bucket_id=NEW.bucket_id
       WHERE c.source_type='reserve_event' AND c.operation_id=NEW.operation_id AND NEW.generation_id=b.generation_id
       AND NEW.role=upper(s.event_type)||'_'||CASE c.used WHEN 0 THEN 'POINT' WHEN 1 THEN 'RESERVE' WHEN 2 THEN 'YES' WHEN 3 THEN 'NO' END
       AND NEW.balance_before=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=b.bucket_id ORDER BY e.event_seq DESC LIMIT 1),b.opening_value)
       AND ((c.used=0 AND b.kind='POINT_AVAILABLE' AND b.account_id=c.account_id AND NEW.delta=CASE WHEN s.event_type='split' THEN -s.points_micro ELSE s.points_micro END AND NEW.balance_after=(SELECT available_micro FROM point_accounts WHERE account_id=c.account_id))
         OR (c.used=1 AND b.kind='MARKET_RESERVE' AND b.market_id=c.market_id AND NEW.delta=CASE WHEN s.event_type='split' THEN s.points_micro ELSE -s.points_micro END AND NEW.balance_after=(SELECT reserve_micro FROM market_reserves WHERE market_id=c.market_id))
         OR (c.used IN (2,3) AND b.kind='SHARE_AVAILABLE' AND b.account_id=c.account_id AND b.market_id=c.market_id AND b.outcome=CASE c.used WHEN 2 THEN 'YES' ELSE 'NO' END AND NEW.delta=CASE WHEN s.event_type='split' THEN s.quantity ELSE -s.quantity END AND NEW.balance_after=(SELECT available_shares FROM outcome_positions WHERE account_id=c.account_id AND market_id=c.market_id AND outcome=b.outcome))))
      THEN RAISE(ABORT,'balance_history_event_invalid') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_market_final BEFORE INSERT ON balance_history_operation WHEN NEW.source_type='bucket_create' BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN collateral_markets m ON m.market_id=c.market_id JOIN market_reserves r ON r.market_id=m.market_id
       JOIN balance_history_bucket b ON b.kind='MARKET_RESERVE' AND b.market_id=m.market_id AND b.creation_operation_id=c.operation_id
       WHERE c.operation_id=NEW.operation_id AND c.source_type='bucket_create' AND c.used=1 AND NEW.source_id=c.market_id AND NEW.market_id=c.market_id
       AND NEW.account_id IS NULL AND NEW.amount=0 AND m.engine_key='collateralized_clob_v2' AND m.status='open' AND r.reserve_micro=0 AND b.opening_value=0
       AND (SELECT count(*) FROM balance_history_event WHERE operation_id=c.operation_id)=0
       AND (SELECT count(*) FROM demo_audit_events WHERE event_type='v2_collateral_market_created' AND reference_type='collateral_market' AND reference_id=c.market_id)=1)
      THEN RAISE(ABORT,'balance_history_manifest_invalid') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_reserve_final BEFORE INSERT ON balance_history_operation WHEN NEW.source_type='reserve_event' BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN reserve_events s ON s.id=c.source_id
       JOIN balance_history_event p ON p.operation_id=c.operation_id AND p.role=upper(s.event_type)||'_POINT'
       JOIN balance_history_event r ON r.operation_id=c.operation_id AND r.role=upper(s.event_type)||'_RESERVE'
       JOIN balance_history_event y ON y.operation_id=c.operation_id AND y.role=upper(s.event_type)||'_YES'
       JOIN balance_history_event n ON n.operation_id=c.operation_id AND n.role=upper(s.event_type)||'_NO'
       JOIN balance_history_bucket bp ON bp.bucket_id=p.bucket_id JOIN balance_history_bucket br ON br.bucket_id=r.bucket_id
       JOIN balance_history_bucket byes ON byes.bucket_id=y.bucket_id JOIN balance_history_bucket bno ON bno.bucket_id=n.bucket_id
       JOIN point_accounts a ON a.account_id=c.account_id JOIN market_reserves mr ON mr.market_id=c.market_id
       JOIN outcome_positions py ON py.account_id=c.account_id AND py.market_id=c.market_id AND py.outcome='YES'
       JOIN outcome_positions pn ON pn.account_id=c.account_id AND pn.market_id=c.market_id AND pn.outcome='NO'
       JOIN collateral_ledger_entries lp ON lp.reference_type='reserve_event' AND lp.reference_id=cast(s.id AS TEXT) AND lp.entry_type=CASE s.event_type WHEN 'split' THEN 'split_account_debit' ELSE 'merge_account_credit' END
       JOIN collateral_ledger_entries lr ON lr.reference_type='reserve_event' AND lr.reference_id=cast(s.id AS TEXT) AND lr.entry_type=CASE s.event_type WHEN 'split' THEN 'split_reserve_credit' ELSE 'merge_reserve_debit' END
       WHERE c.operation_id=NEW.operation_id AND c.source_type='reserve_event' AND c.used=4 AND NEW.source_id=s.id AND NEW.account_id=c.account_id AND NEW.market_id=c.market_id
       AND NEW.amount=c.amount AND NEW.payload_hash=s.payload_hash AND s.account_id=c.account_id AND s.market_id=c.market_id AND s.quantity=c.quantity AND s.points_micro=c.amount
       AND bp.kind='POINT_AVAILABLE' AND bp.account_id=c.account_id AND br.kind='MARKET_RESERVE' AND br.market_id=c.market_id
       AND byes.kind='SHARE_AVAILABLE' AND byes.account_id=c.account_id AND byes.market_id=c.market_id AND byes.outcome='YES'
       AND bno.kind='SHARE_AVAILABLE' AND bno.account_id=c.account_id AND bno.market_id=c.market_id AND bno.outcome='NO'
       AND p.delta=CASE WHEN s.event_type='split' THEN -s.points_micro ELSE s.points_micro END
       AND r.delta=-p.delta AND y.delta=CASE WHEN s.event_type='split' THEN s.quantity ELSE -s.quantity END AND n.delta=y.delta
       AND r.balance_before=s.reserve_before_micro AND r.balance_after=s.reserve_after_micro
       AND a.available_micro=p.balance_after AND mr.reserve_micro=r.balance_after AND py.available_shares=y.balance_after AND pn.available_shares=n.balance_after
       AND lp.engine_key=s.engine_key AND lp.account_id=c.account_id AND lp.market_id=c.market_id AND lp.amount_micro=p.delta AND lp.account_available_before_micro=p.balance_before AND lp.account_available_after_micro=p.balance_after AND lp.reserve_before_micro IS NULL AND lp.reserve_after_micro IS NULL
       AND lr.engine_key=s.engine_key AND lr.account_id=c.account_id AND lr.market_id=c.market_id AND lr.amount_micro=r.delta AND lr.reserve_before_micro=r.balance_before AND lr.reserve_after_micro=r.balance_after AND lr.account_available_before_micro IS NULL AND lr.account_available_after_micro IS NULL
       AND (SELECT count(*) FROM balance_history_event WHERE operation_id=c.operation_id)=4
       AND (SELECT count(*) FROM collateral_ledger_entries WHERE reference_type='reserve_event' AND reference_id=cast(s.id AS TEXT))=2
       AND (SELECT count(*) FROM demo_audit_events WHERE event_type=CASE s.event_type WHEN 'split' THEN 'v2_complete_sets_split' ELSE 'v2_complete_sets_merged' END AND reference_type='reserve_event' AND reference_id=cast(s.id AS TEXT))=1)
      THEN RAISE(ABORT,'balance_history_manifest_invalid') END; END''')
    for action in ('UPDATE','DELETE'):
        conn.execute(f'''CREATE TRIGGER balance_history_reserve_source_final_{action.lower()} BEFORE {action} ON reserve_events
          WHEN EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type='reserve_event' AND o.source_id=OLD.id)
          BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    for table in ('collateral_ledger_entries','demo_audit_events'):
        for action in ('INSERT','UPDATE','DELETE'):
            prefix='NEW' if action=='INSERT' else 'OLD'
            conn.execute(f'''CREATE TRIGGER balance_history_reserve_{table}_{action.lower()} BEFORE {action} ON {table}
              WHEN {prefix}.reference_type='reserve_event' AND EXISTS(SELECT 1 FROM balance_history_operation o
                WHERE o.source_type='reserve_event' AND CAST(o.source_id AS TEXT)={prefix}.reference_id)
              BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    for action in ('INSERT','UPDATE','DELETE'):
        prefix='NEW' if action=='INSERT' else 'OLD'
        conn.execute(f'''CREATE TRIGGER balance_history_market_audit_{action.lower()} BEFORE {action} ON demo_audit_events
          WHEN {prefix}.reference_type='collateral_market' AND EXISTS(SELECT 1 FROM balance_history_operation o
            WHERE o.source_type='bucket_create' AND o.source_id={prefix}.reference_id)
          BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')


def begin_market(conn, *, market_id):
    if not conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
        raise HistoryError('history_transaction_required')
    op=uuid.uuid4().hex
    conn.execute("INSERT INTO balance_history_context(operation_id,guard,source_type,market_id,amount) VALUES (?,1,'bucket_create',?,0)",(op,market_id))
    return op


def finalize_market(conn, *, operation_id, market_id):
    from app.storage import verify_audit_chain
    if verify_audit_chain(conn)['integrity_status']!='verified': raise HistoryError('invalid_market_audit')
    conn.execute("INSERT INTO balance_history_operation(operation_id,source_type,source_id,market_id,amount,payload_hash) VALUES (?,'bucket_create',?,?,0,'')",(operation_id,market_id,market_id))
    conn.execute('DELETE FROM balance_history_context WHERE operation_id=?',(operation_id,))


def begin_reserve(conn, *, account_id, market_id, event_type, quantity, amount):
    if not conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
        raise HistoryError('history_transaction_required')
    op=uuid.uuid4().hex
    conn.execute("""INSERT INTO balance_history_context(operation_id,guard,source_type,account_id,market_id,event_type,quantity,amount)
                    VALUES (?,1,'reserve_event',?,?,?,?,?)""",(op,account_id,market_id,event_type,quantity,amount))
    return op


def finalize_reserve(conn, *, operation_id, source_id, account_id, market_id, amount, payload_hash):
    from app.storage import verify_audit_chain
    if verify_audit_chain(conn)['integrity_status']!='verified': raise HistoryError('invalid_reserve_audit')
    conn.execute("""INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,market_id,amount,payload_hash)
                    VALUES (?,'reserve_event',?,?,?,?,?)""",(operation_id,source_id,account_id,market_id,amount,payload_hash))
    conn.execute('DELETE FROM balance_history_context WHERE operation_id=?',(operation_id,))


def _install_order_protocol(conn):
    conn.execute('''CREATE TRIGGER balance_history_order_source BEFORE INSERT ON order_collateral_events BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN order_collateral_reservations r ON r.id=c.reservation_id
       JOIN point_accounts a ON a.account_id=r.account_id
       WHERE c.source_type='order_collateral_event' AND c.used=0 AND c.source_id IS NULL
       AND r.contract_version=1 AND r.status='reserved' AND r.engine_key='collateralized_clob_v2'
       AND NEW.engine_key=r.engine_key AND NEW.reservation_id=r.id AND NEW.account_id=r.account_id
       AND c.account_id=r.account_id AND c.market_id=r.market_id AND c.outcome=r.outcome AND c.reservation_id=r.id
       AND c.asset_type=r.collateral_type AND NEW.asset_type=r.collateral_type AND NEW.asset_amount=r.collateral_amount AND NEW.asset_amount=c.amount
       AND NEW.event_type=c.event_type AND NEW.event_type IN ('reserve','release')
       AND NOT EXISTS(SELECT 1 FROM order_collateral_events prior WHERE prior.reservation_id=r.id AND prior.event_type=NEW.event_type)
       AND (NEW.event_type='release' OR EXISTS(SELECT 1 FROM collateral_markets m WHERE m.market_id=r.market_id AND m.engine_key=r.engine_key AND m.status='open'))
       AND ((r.side='BUY' AND r.collateral_type='point') OR (r.side='SELL' AND r.collateral_type='share'))
       AND a.engine_key=r.engine_key AND a.owner_type='participant' AND a.owner_id=r.participant_id
       AND ((NEW.event_type='reserve' AND NEW.release_reason IS NULL AND NEW.available_after-NEW.available_before=-c.amount AND NEW.locked_after-NEW.locked_before=c.amount)
        OR (NEW.event_type='release' AND NEW.release_reason IN ('cancelled','rejected') AND NEW.available_after-NEW.available_before=c.amount AND NEW.locked_after-NEW.locked_before=-c.amount))
       AND ((c.asset_type='point' AND a.available_micro=NEW.available_before AND a.locked_micro=NEW.locked_before)
         OR (c.asset_type='share' AND EXISTS(SELECT 1 FROM outcome_positions p WHERE p.account_id=c.account_id AND p.market_id=c.market_id AND p.outcome=c.outcome AND p.available_shares=NEW.available_before AND p.locked_shares=NEW.locked_before))))
      THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_order_source_after AFTER INSERT ON order_collateral_events BEGIN
      UPDATE balance_history_context SET source_id=NEW.id WHERE source_type='order_collateral_event' AND source_id IS NULL AND used=0;
      SELECT CASE WHEN NEW.id<=(SELECT order_watermark FROM balance_history_generation) THEN RAISE(ABORT,'balance_history_source_invalid') END; END''')
    specs=(('point_accounts','point','available_micro','locked_micro',"OLD.account_id=c.account_id AND NEW.account_id=OLD.account_id AND NEW.engine_key=OLD.engine_key AND NEW.owner_type=OLD.owner_type AND NEW.owner_id=OLD.owner_id",'POINT_AVAILABLE','POINT_LOCKED',"b.account_id=OLD.account_id"),
           ('outcome_positions','share','available_shares','locked_shares',"OLD.account_id=c.account_id AND OLD.market_id=c.market_id AND OLD.outcome=c.outcome AND NEW.account_id=OLD.account_id AND NEW.market_id=OLD.market_id AND NEW.outcome=OLD.outcome",'SHARE_AVAILABLE','SHARE_LOCKED',"b.account_id=OLD.account_id AND b.market_id=OLD.market_id AND b.outcome=OLD.outcome"))
    for table,asset,available,locked,identity,kind_a,kind_l,bucket in specs:
        when="WHEN EXISTS(SELECT 1 FROM balance_history_context WHERE source_type='order_collateral_event')"
        conn.execute(f'''CREATE TRIGGER balance_history_order_{table}_update BEFORE UPDATE ON {table} {when} BEGIN
          SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN order_collateral_events s ON s.id=c.source_id
           JOIN balance_history_bucket ba ON ba.kind='{kind_a}' AND ba.account_id=OLD.account_id {'AND ba.market_id=OLD.market_id AND ba.outcome=OLD.outcome' if asset=='share' else ''}
           JOIN balance_history_bucket bl ON bl.kind='{kind_l}' AND bl.account_id=OLD.account_id {'AND bl.market_id=OLD.market_id AND bl.outcome=OLD.outcome' if asset=='share' else ''}
           WHERE c.source_type='order_collateral_event' AND c.asset_type='{asset}' AND c.used=0 AND {identity}
           AND s.id>(SELECT order_watermark FROM balance_history_generation) AND s.reservation_id=c.reservation_id AND s.account_id=c.account_id AND s.asset_type=c.asset_type AND s.asset_amount=c.amount AND s.event_type=c.event_type
           AND typeof(NEW.{available})='integer' AND typeof(NEW.{locked})='integer' AND NEW.{available}>=0 AND NEW.{locked}>=0
           AND OLD.{available}=s.available_before AND NEW.{available}=s.available_after AND OLD.{locked}=s.locked_before AND NEW.{locked}=s.locked_after
           AND OLD.{available}=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=ba.bucket_id ORDER BY e.event_seq DESC LIMIT 1),ba.opening_value)
           AND OLD.{locked}=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=bl.bucket_id ORDER BY e.event_seq DESC LIMIT 1),bl.opening_value))
          THEN RAISE(ABORT,'balance_history_source_protocol_unavailable') END; END''')
        for kind,field,role in ((kind_a,available,'ORDER_AVAILABLE'),(kind_l,locked,'ORDER_LOCKED')):
            if role=='ORDER_AVAILABLE':
                conn.execute(f'''CREATE TRIGGER balance_history_order_{table}_update_after AFTER UPDATE ON {table} {when} BEGIN
                  INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after)
                  SELECT b.generation_id,b.bucket_id,c.operation_id,'ORDER_AVAILABLE',OLD.{available},NEW.{available}-OLD.{available},NEW.{available}
                  FROM balance_history_bucket b JOIN balance_history_context c ON c.source_type='order_collateral_event' WHERE b.kind='{kind_a}' AND {bucket};
                  INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after)
                  SELECT b.generation_id,b.bucket_id,c.operation_id,'ORDER_LOCKED',OLD.{locked},NEW.{locked}-OLD.{locked},NEW.{locked}
                  FROM balance_history_bucket b JOIN balance_history_context c ON c.source_type='order_collateral_event' WHERE b.kind='{kind_l}' AND {bucket};
                  UPDATE balance_history_context SET used=2 WHERE source_type='order_collateral_event'; END''')
    conn.execute('''CREATE TRIGGER balance_history_order_event BEFORE INSERT ON balance_history_event WHEN NEW.role IN ('ORDER_AVAILABLE','ORDER_LOCKED') BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN order_collateral_events s ON s.id=c.source_id
       JOIN balance_history_bucket b ON b.bucket_id=NEW.bucket_id JOIN order_collateral_reservations r ON r.id=c.reservation_id
       WHERE c.source_type='order_collateral_event' AND c.used=0 AND c.operation_id=NEW.operation_id AND s.id>(SELECT order_watermark FROM balance_history_generation)
       AND b.generation_id=NEW.generation_id AND b.account_id=c.account_id AND s.reservation_id=r.id
       AND ((c.asset_type='point' AND b.kind=CASE NEW.role WHEN 'ORDER_AVAILABLE' THEN 'POINT_AVAILABLE' ELSE 'POINT_LOCKED' END AND b.market_id IS NULL)
         OR (c.asset_type='share' AND b.kind=CASE NEW.role WHEN 'ORDER_AVAILABLE' THEN 'SHARE_AVAILABLE' ELSE 'SHARE_LOCKED' END AND b.market_id=c.market_id AND b.outcome=c.outcome))
       AND NEW.balance_before=coalesce((SELECT e.balance_after FROM balance_history_event e WHERE e.bucket_id=b.bucket_id ORDER BY event_seq DESC LIMIT 1),b.opening_value)
       AND NEW.balance_before=CASE NEW.role WHEN 'ORDER_AVAILABLE' THEN s.available_before ELSE s.locked_before END
       AND NEW.balance_after=CASE NEW.role WHEN 'ORDER_AVAILABLE' THEN s.available_after ELSE s.locked_after END
       AND ((c.asset_type='point' AND NEW.balance_after=(SELECT CASE NEW.role WHEN 'ORDER_AVAILABLE' THEN available_micro ELSE locked_micro END FROM point_accounts WHERE account_id=c.account_id))
         OR (c.asset_type='share' AND NEW.balance_after=(SELECT CASE NEW.role WHEN 'ORDER_AVAILABLE' THEN available_shares ELSE locked_shares END FROM outcome_positions WHERE account_id=c.account_id AND market_id=c.market_id AND outcome=c.outcome))))
      THEN RAISE(ABORT,'balance_history_event_invalid') END; END''')
    conn.execute('''CREATE TRIGGER balance_history_order_final BEFORE INSERT ON balance_history_operation WHEN NEW.source_type='order_collateral_event' BEGIN
      SELECT CASE WHEN NOT EXISTS(SELECT 1 FROM balance_history_context c JOIN order_collateral_events s ON s.id=c.source_id
       JOIN order_collateral_reservations r ON r.id=c.reservation_id
       JOIN balance_history_event ea ON ea.operation_id=c.operation_id AND ea.role='ORDER_AVAILABLE'
       JOIN balance_history_event el ON el.operation_id=c.operation_id AND el.role='ORDER_LOCKED'
       JOIN balance_history_bucket ba ON ba.bucket_id=ea.bucket_id JOIN balance_history_bucket bl ON bl.bucket_id=el.bucket_id
       JOIN order_collateral_ledger_entries la ON la.event_id=s.id AND la.balance_bucket='available'
       JOIN order_collateral_ledger_entries ll ON ll.event_id=s.id AND ll.balance_bucket='locked'
       WHERE c.source_type=NEW.source_type AND c.operation_id=NEW.operation_id AND c.used=2 AND NEW.source_id=s.id
       AND NEW.account_id=c.account_id AND NEW.market_id=c.market_id AND NEW.amount=c.amount AND NEW.payload_hash=s.payload_hash
       AND s.reservation_id=r.id AND s.account_id=c.account_id AND s.asset_type=c.asset_type AND s.asset_amount=c.amount AND s.event_type=c.event_type
       AND r.engine_key='collateralized_clob_v2' AND r.account_id=c.account_id AND r.market_id=c.market_id AND r.outcome=c.outcome AND r.contract_version=1
       AND ((s.event_type='reserve' AND r.status='reserved' AND r.remaining_collateral=c.amount AND r.released_collateral=0)
         OR (s.event_type='release' AND r.status='released' AND r.release_reason=s.release_reason AND r.remaining_collateral=0 AND r.released_collateral=c.amount))
       AND ba.account_id=c.account_id AND bl.account_id=c.account_id
       AND ((c.asset_type='point' AND ba.kind='POINT_AVAILABLE' AND bl.kind='POINT_LOCKED'
          AND ea.balance_after=(SELECT available_micro FROM point_accounts WHERE account_id=c.account_id)
          AND el.balance_after=(SELECT locked_micro FROM point_accounts WHERE account_id=c.account_id))
        OR (c.asset_type='share' AND ba.kind='SHARE_AVAILABLE' AND bl.kind='SHARE_LOCKED' AND ba.market_id=c.market_id AND bl.market_id=c.market_id AND ba.outcome=c.outcome AND bl.outcome=c.outcome
          AND ea.balance_after=(SELECT available_shares FROM outcome_positions WHERE account_id=c.account_id AND market_id=c.market_id AND outcome=c.outcome)
          AND el.balance_after=(SELECT locked_shares FROM outcome_positions WHERE account_id=c.account_id AND market_id=c.market_id AND outcome=c.outcome)))
       AND ea.balance_before=s.available_before AND ea.balance_after=s.available_after AND el.balance_before=s.locked_before AND el.balance_after=s.locked_after
       AND la.engine_key=s.engine_key AND la.reservation_id=r.id AND la.account_id=c.account_id AND la.market_id=c.market_id AND la.outcome=c.outcome AND la.asset_type=c.asset_type AND la.delta=ea.delta AND la.balance_before=ea.balance_before AND la.balance_after=ea.balance_after
       AND ll.engine_key=s.engine_key AND ll.reservation_id=r.id AND ll.account_id=c.account_id AND ll.market_id=c.market_id AND ll.outcome=c.outcome AND ll.asset_type=c.asset_type AND ll.delta=el.delta AND ll.balance_before=el.balance_before AND ll.balance_after=el.balance_after
       AND ea.event_seq<el.event_seq AND (SELECT count(*) FROM balance_history_event WHERE operation_id=c.operation_id)=2
       AND (SELECT count(*) FROM order_collateral_ledger_entries WHERE event_id=s.id)=2
       AND (SELECT count(*) FROM demo_audit_events WHERE reference_type='order_collateral_reservation' AND reference_id=cast(r.id AS TEXT)
         AND event_type=CASE WHEN s.event_type='reserve' THEN 'v2_order_collateral_reserved' WHEN s.release_reason='cancelled' THEN 'v2_order_collateral_cancelled' ELSE 'v2_order_collateral_rejected' END
         AND user_id=r.participant_id AND json_valid(after_json)=1
         AND json_extract(after_json,'$.market_id')=r.market_id AND json_extract(after_json,'$.side')=r.side
         AND json_extract(after_json,'$.outcome')=r.outcome AND json_extract(after_json,'$.quantity')=r.quantity
         AND json_extract(after_json,'$.limit_price_micro')=r.limit_price_micro AND json_extract(after_json,'$.collateral_type')=r.collateral_type
         AND json_extract(after_json,'$.collateral_amount')=r.collateral_amount AND json_extract(after_json,'$.status')=r.status
         AND ((s.event_type='reserve' AND json_type(after_json,'$.release_reason') IS NULL)
           OR (s.event_type='release' AND json_extract(after_json,'$.release_reason')=s.release_reason)))=1)
      THEN RAISE(ABORT,'balance_history_manifest_invalid') END; END''')
    for table,cond in (
      ('order_collateral_events',"EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type='order_collateral_event' AND o.source_id=OLD.id)"),
      ('order_collateral_ledger_entries',"EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type='order_collateral_event' AND o.source_id=OLD.event_id)"),
    ):
        for action in ('UPDATE','DELETE'):
            conn.execute(f'''CREATE TRIGGER balance_history_order_{table}_{action.lower()} BEFORE {action} ON {table} WHEN {cond}
              BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    conn.execute('''CREATE TRIGGER balance_history_order_ledger_insert BEFORE INSERT ON order_collateral_ledger_entries
      WHEN EXISTS(SELECT 1 FROM balance_history_operation o WHERE o.source_type='order_collateral_event' AND o.source_id=NEW.event_id)
      BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')
    for action in ('INSERT','UPDATE','DELETE'):
        prefix='NEW' if action=='INSERT' else 'OLD'
        conn.execute(f'''CREATE TRIGGER balance_history_order_audit_{action.lower()} BEFORE {action} ON demo_audit_events
          WHEN {prefix}.reference_type='order_collateral_reservation' AND EXISTS(SELECT 1 FROM balance_history_operation o JOIN order_collateral_events s ON s.id=o.source_id
           WHERE o.source_type='order_collateral_event' AND CAST(s.reservation_id AS TEXT)={prefix}.reference_id
           AND {prefix}.event_type=CASE WHEN s.event_type='reserve' THEN 'v2_order_collateral_reserved' WHEN s.release_reason='cancelled' THEN 'v2_order_collateral_cancelled' ELSE 'v2_order_collateral_rejected' END)
          BEGIN SELECT RAISE(ABORT,'balance_history_source_immutable'); END''')


def begin_order(conn, *, reservation, event_type):
    if not conn.in_transaction or conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
        raise HistoryError('history_transaction_required')
    op=uuid.uuid4().hex
    conn.execute("""INSERT INTO balance_history_context(operation_id,guard,source_type,account_id,market_id,outcome,asset_type,reservation_id,amount,event_type)
                    VALUES (?,1,'order_collateral_event',?,?,?,?,?,?,?)""",(op,reservation['account_id'],reservation['market_id'],reservation['outcome'],reservation['collateral_type'],reservation['id'],reservation['collateral_amount'],event_type))
    return op


def finalize_order(conn, *, operation_id, source_id, reservation, payload_hash):
    from app.storage import verify_audit_chain
    if verify_audit_chain(conn)['integrity_status']!='verified': raise HistoryError('invalid_order_audit')
    conn.execute("""INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,market_id,amount,payload_hash)
                    VALUES (?,'order_collateral_event',?,?,?,?,?)""",(operation_id,source_id,reservation['account_id'],reservation['market_id'],reservation['collateral_amount'],payload_hash))
    conn.execute('DELETE FROM balance_history_context WHERE operation_id=?',(operation_id,))


def _history_objects(conn):
    return {(row[0],row[1]):(row[2],row[3]) for row in conn.execute(
        "SELECT type,name,tbl_name,sql FROM sqlite_master WHERE name LIKE 'balance_history_%' AND sql IS NOT NULL")}


def _history_schema_reference():
    """Construct the expected history DDL in an isolated in-memory V2 DB."""
    from functools import lru_cache
    return _cached_history_schema_reference()


from functools import lru_cache


@lru_cache(maxsize=1)
def _cached_history_schema_reference():
    from app.storage import connect, init_db
    from app.v2_clob_schema import TABLES, INDEXES, TRIGGERS, _canonical
    c=connect(':memory:')
    try:
        init_db(c)
        c.execute('PRAGMA foreign_keys=OFF')
        c.execute('BEGIN')
        old=('order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries')
        for table in reversed(old): c.execute('DROP TABLE '+table)
        for table in old: c.execute(TABLES[table])
        for table in ('v2_orders','v2_trades','v2_fills','v2_trade_ledger_entries','v2_command_results','v2_priority_sequence'): c.execute(TABLES[table])
        for sql in INDEXES.values(): c.execute(sql)
        for sql in TRIGGERS.values(): c.execute(sql)
        c.execute('INSERT INTO v2_priority_sequence VALUES (1,0)')
        c.execute('PRAGMA user_version=2'); c.commit()
        c.execute('PRAGMA foreign_keys=ON'); c.execute('BEGIN')
        install_checkpoint(c)
        return {key:(value[0],_canonical(value[1])) for key,value in _history_objects(c).items()}
    finally:
        c.close()


def _dicts(conn, sql, args=()):
    cur=conn.execute(sql,args)
    names=[item[0] for item in cur.description]
    return [dict(zip(names,tuple(row))) for row in cur]


def verify_history(conn):
    """Check history without changing persistent state, in one read snapshot."""
    codes=[]
    def fail(code):
        if code not in codes: codes.append(code)
    savepoint='history_read_'+uuid.uuid4().hex
    try:
        conn.execute('SAVEPOINT '+savepoint)
        _verify_history_impl(conn,fail)
    except (sqlite3.Error,ValueError,TypeError,KeyError,OverflowError,HistoryError):
        fail('history_invalid_data')
    finally:
        if conn.in_transaction:
            try: conn.execute('RELEASE SAVEPOINT '+savepoint)
            except sqlite3.Error: fail('history_snapshot_error')
    return {'integrity_status':'failed' if codes else 'verified','violation_count':len(codes),'violation_codes':codes}


def _verify_history_impl(conn,fail):
    from app.v2_clob_schema import _canonical
    objects=_history_objects(conn)
    if not objects:
        fail('history_missing'); return
    expected=_history_schema_reference()
    actual={key:(value[0],_canonical(value[1])) for key,value in objects.items()}
    if actual!=expected:
        fail('history_schema_invalid'); return
    if conn.execute('PRAGMA user_version').fetchone()[0]!=2: fail('history_schema_version_invalid')
    if conn.execute('PRAGMA foreign_keys').fetchone()[0]!=1:
        from app.v2_clob_migration import _history_migration_connection_authorized
        if not _history_migration_connection_authorized(conn): fail('history_foreign_keys_disabled')
    if conn.execute('PRAGMA foreign_key_check').fetchone() is not None: fail('history_foreign_key_invalid')
    if conn.execute('PRAGMA integrity_check').fetchone()[0]!='ok': fail('history_integrity_invalid')
    if conn.execute('SELECT 1 FROM balance_history_context LIMIT 1').fetchone() is not None: fail('history_unfinalized')
    if conn.execute('SELECT 1 FROM balance_history_context_must_be_empty LIMIT 1').fetchone() is not None: fail('history_unfinalized')
    generation=_dicts(conn,'SELECT * FROM balance_history_generation')
    if len(generation)!=1:
        fail('history_generation_invalid'); return
    gen=generation[0];gid=gen['generation_id']
    if not isinstance(gid,str) or not gid or gen['format_version']!=1 or gen['finalized']!=1: fail('history_generation_invalid')
    buckets=_dicts(conn,'SELECT * FROM balance_history_bucket')
    checkpoint=[[b['kind'],b['account_id'],b['market_id'],b['outcome'],b['opening_value']] for b in buckets if b['origin']=='checkpoint']
    checkpoint.sort(key=lambda row:_json(row[:4]).encode('utf-8'))
    count_fields={'POINT_AVAILABLE':'point_available_count','POINT_LOCKED':'point_locked_count','SHARE_AVAILABLE':'share_available_count','SHARE_LOCKED':'share_locked_count','MARKET_RESERVE':'market_reserve_count'}
    if (type(gen['checkpoint_count']) is not int or len(checkpoint)!=gen['checkpoint_count'] or _digest(gid,checkpoint)!=gen['checkpoint_sha256']
        or any(type(gen[field]) is not int or gen[field]!=sum(row[0]==kind for row in checkpoint) for kind,field in count_fields.items())): fail('history_checkpoint_invalid')
    boundary_id=gen['audit_boundary_id'];boundary_hash=gen['audit_boundary_hash']
    if type(boundary_id) is not int or boundary_id<0 or (boundary_id==0 and boundary_hash is not None) or (boundary_id>0 and (not isinstance(boundary_hash,str) or conn.execute('SELECT 1 FROM demo_audit_events WHERE id=? AND event_hash=?',(boundary_id,boundary_hash)).fetchone() is None)):
        fail('history_checkpoint_invalid')
    live={tuple(row[:4]):row[4] for row in _checkpoint(conn)}
    keys={};by_id={}
    for b in buckets:
        key=(b['kind'],b['account_id'],b['market_id'],b['outcome'])
        if key in keys or b['generation_id']!=gid: fail('history_bucket_invalid')
        keys[key]=b;by_id[b['bucket_id']]=b
        if b['origin']=='created' and (b['opening_value']!=0 or b['creation_operation_id'] is None): fail('history_bucket_invalid')
    if set(keys)!=set(live): fail('history_bucket_coverage_invalid')
    for key,b in keys.items():
        kind,account,market,outcome=key
        if kind=='POINT_AVAILABLE': paired=('POINT_LOCKED',account,None,None)
        elif kind=='SHARE_AVAILABLE': paired=('SHARE_LOCKED',account,market,outcome)
        else: continue
        other=keys.get(paired)
        if other is None or other['origin']!=b['origin'] or (b['origin']=='created' and other['creation_operation_id']!=b['creation_operation_id']):
            fail('history_bucket_creation_invalid')
    current={bid:b['opening_value'] for bid,b in by_id.items()}
    events=_dicts(conn,'SELECT * FROM balance_history_event ORDER BY event_seq')
    grouped={}
    previous=0
    for e in events:
        seq=e['event_seq'];bid=e['bucket_id'];delta=e['delta'];before=e['balance_before'];after=e['balance_after']
        if any(type(v) is not int for v in (seq,delta,before,after)) or seq<=previous or not -9223372036854775807<=delta<=9223372036854775807 or not 0<=before<=9223372036854775807 or not 0<=after<=9223372036854775807:
            fail('history_event_invalid');continue
        previous=seq
        if bid not in current or e['generation_id']!=gid or current[bid]!=before or before+delta!=after: fail('history_chain_invalid')
        current[bid]=after
        grouped.setdefault(e['operation_id'],[]).append(e)
    for bid,b in by_id.items():
        key=(b['kind'],b['account_id'],b['market_id'],b['outcome'])
        if key not in live or current.get(bid)!=live.get(key): fail('history_live_balance_mismatch')
    _verify_history_sources(conn,gen,by_id,grouped,fail)


def _verify_history_sources(conn,gen,buckets,grouped,fail):
    from collections import Counter
    from app.storage import verify_audit_chain
    from app.collateral_ledger import _payload_hash
    old_factory=conn.row_factory
    try:
        conn.row_factory=sqlite3.Row
        audit_status=verify_audit_chain(conn)['integrity_status']
    finally:
        conn.row_factory=old_factory
    if audit_status not in ('verified','empty'):
        fail('history_audit_invalid')
    boundary_id=gen['audit_boundary_id']
    if type(boundary_id) is not int or boundary_id<0:
        fail('history_checkpoint_invalid');boundary_id=0
    operations=_dicts(conn,'SELECT * FROM balance_history_operation')
    op_ids={op['operation_id'] for op in operations}
    if set(grouped)-op_ids: fail('history_unfinalized')
    by_type={}
    for op in operations:
        by_type.setdefault(op['source_type'],[]).append(op['source_id'])
    specs=(('point_supply_event','point_supply_events','supply_watermark'),('point_allocation_event','point_allocation_events','allocation_watermark'),('reserve_event','reserve_events','reserve_watermark'),('order_collateral_event','order_collateral_events','order_watermark'))
    for typ,table,watermark in specs:
        actual=[r[0] for r in conn.execute('SELECT id FROM '+table+' WHERE id>?',(gen[watermark],))]
        if Counter(actual)!=Counter(by_type.get(typ,[])): fail('history_source_coverage_invalid')
    op_by_id={op['operation_id']:op for op in operations}
    first_event_by_bucket={}
    for event_rows in grouped.values():
        for event in event_rows:
            bucket_id=event['bucket_id']
            first_event_by_bucket[bucket_id]=min(first_event_by_bucket.get(bucket_id,event['event_seq']),event['event_seq'])
    for b in buckets.values():
        if b['origin']!='created': continue
        op=op_by_id.get(b['creation_operation_id'])
        if op is None:
            fail('history_bucket_invalid');continue
        typ=op['source_type'];valid=False
        if b['kind'] in ('POINT_AVAILABLE','POINT_LOCKED'):
            if typ=='point_supply_event':
                row=conn.execute('SELECT destination_account_id FROM point_supply_events WHERE id=?',(op['source_id'],)).fetchone()
                valid=row is not None and row[0]==b['account_id']
            elif typ=='point_allocation_event':
                row=conn.execute('SELECT destination_account_id FROM point_allocation_events WHERE id=?',(op['source_id'],)).fetchone()
                valid=row is not None and row[0]==b['account_id']
        elif b['kind'] in ('SHARE_AVAILABLE','SHARE_LOCKED') and typ=='reserve_event':
            row=conn.execute('SELECT account_id,market_id,event_type FROM reserve_events WHERE id=?',(op['source_id'],)).fetchone()
            valid=row is not None and tuple(row)==(b['account_id'],b['market_id'],'split') and b['outcome'] in ('YES','NO')
        elif b['kind']=='MARKET_RESERVE' and typ=='bucket_create':
            valid=op['source_id']==b['market_id'] and op['market_id']==b['market_id']
        if not valid: fail('history_bucket_creation_invalid')
        creating=grouped.get(op['operation_id'],[])
        earliest=min((e['event_seq'] for e in creating),default=None)
        first_event=first_event_by_bucket.get(b['bucket_id'])
        if earliest is not None and first_event is not None and first_event<earliest:
            fail('history_bucket_creation_invalid')
    if conn.execute('SELECT 1 FROM v2_trades LIMIT 1').fetchone() is not None:
        fail('history_trade_source_unsupported')
    def bucket_of(e):
        return buckets.get(e['bucket_id'],{})
    def roles(op,expected):
        es=grouped.get(op['operation_id'],[])
        if Counter(e['role'] for e in es)!=Counter(expected): fail('history_role_invalid')
        if [e['role'] for e in es]!=expected: fail('history_role_order_invalid')
        return {e['role']:e for e in es}
    def check_event(e,kind,account,market,outcome,before,delta,after):
        if not e: fail('history_role_invalid');return
        b=bucket_of(e)
        if (b.get('kind'),b.get('account_id'),b.get('market_id'),b.get('outcome'))!=(kind,account,market,outcome): fail('history_source_mismatch')
        if before is not None and e['balance_before']!=before: fail('history_source_mismatch')
        if delta is not None and e['delta']!=delta: fail('history_source_mismatch')
        if after is not None and e['balance_after']!=after: fail('history_source_mismatch')
    def source(table,source_id):
        found=_dicts(conn,'SELECT * FROM '+table+' WHERE id=?',(source_id,))
        if len(found)!=1: fail('history_source_missing');return None
        return found[0]
    def ledger(typ,ident):
        return _dicts(conn,'SELECT * FROM collateral_ledger_entries WHERE reference_type=? AND reference_id=?',(typ,str(ident)))
    audit_entries={}
    for entry in _dicts(conn,'SELECT id,user_id,request_id,after_json,reference_type,reference_id,event_type FROM demo_audit_events'):
        key=(entry['reference_type'],entry['reference_id'],entry['event_type'])
        audit_entries.setdefault(key,[]).append(entry)
    def audit(typ,ident,event,expected_after=None,user=None,request_id=None):
        entries=audit_entries.get((typ,str(ident),event),[])
        if len(entries)!=1:
            fail('history_audit_mismatch');return
        row=entries[0]
        if row['id']<=boundary_id: fail('history_audit_mismatch')
        if row['user_id']!=user or row['request_id']!=request_id: fail('history_audit_mismatch')
        if expected_after is not None:
            try: actual=json.loads(row['after_json'])
            except (TypeError,ValueError): fail('history_audit_mismatch');return
            if actual!=expected_after: fail('history_audit_mismatch')
    for op in operations:
        typ=op['source_type'];ident=op['source_id'];es=grouped.get(op['operation_id'],[])
        if type(op['amount']) is not int or not 0<=op['amount']<=9223372036854775807: fail('history_integer_invalid')
        if typ=='bucket_create':
            roles(op,[])
            if op['source_id']!=op['market_id'] or op['account_id'] is not None or op['amount']!=0: fail('history_source_mismatch')
            if conn.execute('SELECT 1 FROM collateral_markets WHERE market_id=? AND engine_key=?',(ident,'collateralized_clob_v2')).fetchone() is None: fail('history_source_missing')
            created=[b for b in buckets.values() if b['creation_operation_id']==op['operation_id']]
            if len(created)!=1 or created[0]['kind']!='MARKET_RESERVE' or created[0]['market_id']!=ident: fail('history_bucket_invalid')
            audit('collateral_market',ident,'v2_collateral_market_created',{'engine_key':'collateralized_clob_v2','status':'open'})
        elif typ=='point_supply_event':
            e=roles(op,['BOOTSTRAP_CREDIT']);src=source('point_supply_events',ident)
            if src is None: continue
            ls=ledger(typ,ident)
            if len(ls)!=1: fail('history_ledger_mismatch');continue
            l=ls[0];account=src['destination_account_id'];amount=src['amount_micro']
            if type(amount) is not int or not 0<amount<=9223372036854775807: fail('history_integer_invalid');continue
            if l['engine_key']!=src['engine_key'] or l['market_id'] is not None or l['reserve_before_micro'] is not None or l['reserve_after_micro'] is not None: fail('history_ledger_mismatch')
            owner=conn.execute('SELECT engine_key,owner_type,owner_id FROM point_accounts WHERE account_id=?',(account,)).fetchone()
            if owner is None or tuple(owner)!=('collateralized_clob_v2','operator','operator-treasury'): fail('history_source_mismatch')
            if src['event_type']!='bootstrap_issue' or src['engine_key']!='collateralized_clob_v2' or op['account_id']!=account or op['amount']!=amount or op['payload_hash']!=src['payload_hash'] or src['payload_hash']!=_payload_hash({'operation':'bootstrap','amount_micro':amount}) or l['entry_type']!='bootstrap_issue' or l['account_id']!=account or l['amount_micro']!=amount or l['request_id']!=src['request_id']: fail('history_source_mismatch')
            check_event(e.get('BOOTSTRAP_CREDIT'),'POINT_AVAILABLE',account,None,None,l['account_available_before_micro'],amount,l['account_available_after_micro'])
            audit(typ,ident,'v2_point_supply_bootstrapped',{'engine_key':'collateralized_clob_v2','amount_micro':amount},request_id=src['request_id'])
        elif typ=='point_allocation_event':
            e=roles(op,['ALLOCATION_DEBIT','ALLOCATION_CREDIT']);src=source('point_allocation_events',ident)
            if src is None: continue
            amount=src['amount_micro'];a=src['source_account_id'];b=src['destination_account_id']
            if type(amount) is not int or not 0<amount<=9223372036854775807: fail('history_integer_invalid');continue
            if src['engine_key']!='collateralized_clob_v2' or op['account_id']!=a or op['amount']!=amount or op['payload_hash']!=src['payload_hash'] or src['payload_hash']!=_payload_hash({'operation':'participant_allocation','participant_id':src['participant_id'],'amount_micro':amount}): fail('history_source_mismatch')
            owner_a=conn.execute('SELECT engine_key,owner_type,owner_id FROM point_accounts WHERE account_id=?',(a,)).fetchone()
            owner_b=conn.execute('SELECT engine_key,owner_type,owner_id FROM point_accounts WHERE account_id=?',(b,)).fetchone()
            if owner_a is None or tuple(owner_a)!=('collateralized_clob_v2','operator','operator-treasury') or owner_b is None or tuple(owner_b)!=('collateralized_clob_v2','participant',src['participant_id']): fail('history_source_mismatch')
            check_event(e.get('ALLOCATION_DEBIT'),'POINT_AVAILABLE',a,None,None,src['source_before_micro'],-amount,src['source_after_micro'])
            check_event(e.get('ALLOCATION_CREDIT'),'POINT_AVAILABLE',b,None,None,src['destination_before_micro'],amount,src['destination_after_micro'])
            ls=ledger(typ,ident)
            expected={(a,'participant_allocation_debit',-amount,src['source_before_micro'],src['source_after_micro']),(b,'participant_allocation_credit',amount,src['destination_before_micro'],src['destination_after_micro'])}
            if len(ls)!=2 or {(l['account_id'],l['entry_type'],l['amount_micro'],l['account_available_before_micro'],l['account_available_after_micro']) for l in ls}!=expected or any(l['engine_key']!=src['engine_key'] or l['market_id'] is not None or l['reserve_before_micro'] is not None or l['reserve_after_micro'] is not None or l['request_id']!=src['request_id'] for l in ls): fail('history_ledger_mismatch')
            audit(typ,ident,'v2_participant_points_allocated',{'engine_key':'collateralized_clob_v2','amount_micro':amount},src['participant_id'],src['request_id'])
        elif typ=='reserve_event':
            src=source('reserve_events',ident)
            if src is None: continue
            direction=src['event_type'];prefix=direction.upper();a=src['account_id'];m=src['market_id'];q=src['quantity'];points=src['points_micro']
            if type(q) is not int or type(points) is not int or not 0<q<=9223372036854775807 or not 0<points<=9223372036854775807: fail('history_integer_invalid');continue
            if direction not in ('split','merge') or src['engine_key']!='collateralized_clob_v2' or op['account_id']!=a or op['market_id']!=m or op['amount']!=points or op['payload_hash']!=src['payload_hash'] or src['payload_hash']!=_payload_hash({'operation':direction,'account_id':a,'market_id':m,'quantity':q}) or points!=q*10000: fail('history_source_mismatch');continue
            owner=conn.execute('SELECT engine_key FROM point_accounts WHERE account_id=?',(a,)).fetchone()
            market_owner=conn.execute('SELECT engine_key FROM collateral_markets WHERE market_id=?',(m,)).fetchone()
            if owner is None or owner[0]!='collateralized_clob_v2' or market_owner is None or market_owner[0]!='collateralized_clob_v2': fail('history_source_mismatch')
            e=roles(op,[prefix+'_POINT',prefix+'_RESERVE',prefix+'_YES',prefix+'_NO']);sign=1 if direction=='split' else -1
            ls=ledger(typ,ident);pentry='split_account_debit' if sign==1 else 'merge_account_credit';rentry='split_reserve_credit' if sign==1 else 'merge_reserve_debit'
            lp=[l for l in ls if l['entry_type']==pentry];lr=[l for l in ls if l['entry_type']==rentry]
            if len(ls)!=2 or len(lp)!=1 or len(lr)!=1: fail('history_ledger_mismatch');continue
            lp=lp[0];lr=lr[0]
            if lp['engine_key']!=src['engine_key'] or lr['engine_key']!=src['engine_key'] or lp['account_id']!=a or lp['market_id']!=m or lp['amount_micro']!=-sign*points or lp['reserve_before_micro'] is not None or lp['reserve_after_micro'] is not None or lr['account_id']!=a or lr['market_id']!=m or lr['amount_micro']!=sign*points or lr['reserve_before_micro']!=src['reserve_before_micro'] or lr['reserve_after_micro']!=src['reserve_after_micro'] or lr['account_available_before_micro'] is not None or lr['account_available_after_micro'] is not None or lp['request_id']!=src['request_id'] or lr['request_id']!=src['request_id']: fail('history_ledger_mismatch')
            check_event(e.get(prefix+'_POINT'),'POINT_AVAILABLE',a,None,None,lp['account_available_before_micro'],-sign*points,lp['account_available_after_micro'])
            check_event(e.get(prefix+'_RESERVE'),'MARKET_RESERVE',None,m,None,src['reserve_before_micro'],sign*points,src['reserve_after_micro'])
            for outcome in ('YES','NO'): check_event(e.get(prefix+'_'+outcome),'SHARE_AVAILABLE',a,m,outcome,None,sign*q,None)
            audit(typ,ident,'v2_complete_sets_split' if sign==1 else 'v2_complete_sets_merged',{'market_id':m,'quantity':q},request_id=src['request_id'])
        elif typ=='order_collateral_event':
            e=roles(op,['ORDER_AVAILABLE','ORDER_LOCKED']);src=source('order_collateral_events',ident)
            if src is None: continue
            rows=_dicts(conn,'SELECT * FROM order_collateral_reservations WHERE id=?',(src['reservation_id'],))
            if len(rows)!=1: fail('history_source_missing');continue
            r=rows[0];a=r['account_id'];m=r['market_id'];outcome=r['outcome'];asset=r['collateral_type'];amount=src['asset_amount']
            if type(amount) is not int or not 0<amount<=9223372036854775807: fail('history_integer_invalid');continue
            owner=conn.execute('SELECT engine_key,owner_type,owner_id FROM point_accounts WHERE account_id=?',(a,)).fetchone()
            reservation_events=_dicts(conn,'SELECT event_type,release_reason FROM order_collateral_events WHERE reservation_id=?',(r['id'],))
            reserves=[item for item in reservation_events if item['event_type']=='reserve']
            releases=[item for item in reservation_events if item['event_type']=='release']
            if (len(reserves)!=1 or r['consumed_collateral']!=0 or r['status'] not in ('reserved','released')
                or (r['status']=='reserved' and (releases or r['remaining_collateral']!=r['collateral_amount'] or r['released_collateral']!=0))
                or (r['status']=='released' and (len(releases)!=1 or releases[0]['release_reason']!=r['release_reason'] or r['remaining_collateral']!=0 or r['released_collateral']!=r['collateral_amount']))):
                fail('history_source_mismatch')
            if (src['engine_key']!='collateralized_clob_v2' or r['engine_key']!=src['engine_key'] or src['event_type'] not in ('reserve','release') or src['account_id']!=a or src['asset_type']!=asset or r['contract_version']!=1 or op['account_id']!=a or op['market_id']!=m or op['amount']!=amount or op['payload_hash']!=src['payload_hash'] or amount!=r['collateral_amount']
                or r['side'] not in ('BUY','SELL') or outcome not in ('YES','NO') or (r['side']=='BUY' and asset!='point') or (r['side']=='SELL' and asset!='share')
                or owner is None or tuple(owner)!=('collateralized_clob_v2','participant',r['participant_id'])
                or (src['event_type']=='reserve' and src['release_reason'] is not None)
                or (src['event_type']=='release' and (src['release_reason'] not in ('cancelled','rejected') or r['status']!='released' or r['release_reason']!=src['release_reason']))): fail('history_source_mismatch')
            if src['event_type']=='reserve':
                expected_payload={'operation':'order_collateral_reserve','participant_id':r['participant_id'],'market_id':m,'side':r['side'],'outcome':outcome,'quantity':r['quantity'],'limit_price_micro':r['limit_price_micro']}
            else:
                expected_payload={'operation':'order_collateral_cancel' if src['release_reason']=='cancelled' else 'order_collateral_reject','reservation_id':r['id']}
                if src['release_reason']=='cancelled': expected_payload['participant_id']=r['participant_id']
            if src['payload_hash']!=_payload_hash(expected_payload): fail('history_source_mismatch')
            kind='POINT_' if asset=='point' else 'SHARE_';market=None if asset=='point' else m;out=None if asset=='point' else outcome
            sign=-1 if src['event_type']=='reserve' else 1
            check_event(e.get('ORDER_AVAILABLE'),kind+'AVAILABLE',a,market,out,src['available_before'],sign*amount,src['available_after'])
            check_event(e.get('ORDER_LOCKED'),kind+'LOCKED',a,market,out,src['locked_before'],-sign*amount,src['locked_after'])
            ls=_dicts(conn,'SELECT * FROM order_collateral_ledger_entries WHERE event_id=?',(ident,))
            expected={(a,m,outcome,asset,'available',sign*amount,src['available_before'],src['available_after']),(a,m,outcome,asset,'locked',-sign*amount,src['locked_before'],src['locked_after'])}
            if len(ls)!=2 or {(l['account_id'],l['market_id'],l['outcome'],l['asset_type'],l['balance_bucket'],l['delta'],l['balance_before'],l['balance_after']) for l in ls}!=expected or any(l['engine_key']!=src['engine_key'] or l['reservation_id']!=r['id'] or l['request_id']!=src['request_id'] for l in ls): fail('history_ledger_mismatch')
            audit_after={'market_id':m,'side':r['side'],'outcome':outcome,'quantity':r['quantity'],'limit_price_micro':r['limit_price_micro'],'collateral_type':asset,'collateral_amount':amount,'status':'reserved' if sign==-1 else 'released'}
            if sign==1: audit_after['release_reason']=src['release_reason']
            audit('order_collateral_reservation',r['id'],'v2_order_collateral_reserved' if sign==-1 else ('v2_order_collateral_cancelled' if src['release_reason']=='cancelled' else 'v2_order_collateral_rejected'),audit_after,r['participant_id'],src['request_id'])
        else:
            fail('history_source_unknown')
