"""Isolated v2 DDL tests. This fixture is not a migration implementation."""
import sqlite3

import pytest

from app.storage import connect, init_db
from app.v2_clob_schema import INDEXES, TABLES, TRIGGERS, validate_v2_schema


def make_empty_fixture(path=':memory:'):
    conn = connect(str(path))
    init_db(conn)
    for table in ('order_collateral_ledger_entries', 'order_collateral_events', 'order_collateral_reservations'):
        assert conn.execute(f'SELECT count(*) FROM {table}').fetchone()[0] == 0
    assert not conn.in_transaction
    conn.execute('PRAGMA foreign_keys=OFF')
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 0
    conn.execute('BEGIN IMMEDIATE')
    try:
        for table in ('order_collateral_ledger_entries', 'order_collateral_events', 'order_collateral_reservations'):
            conn.execute(f'DROP TABLE {table}')
        for sql in TABLES.values():
            conn.execute(sql)
        for sql in INDEXES.values():
            conn.execute(sql)
        for sql in TRIGGERS.values():
            conn.execute(sql)
        conn.execute('INSERT INTO v2_priority_sequence VALUES (1,0)')
        conn.execute('PRAGMA user_version=2')
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.execute('PRAGMA foreign_keys=ON')
    assert conn.execute('PRAGMA foreign_keys').fetchone()[0] == 1
    return conn


@pytest.fixture
def db():
    conn = make_empty_fixture()
    yield conn
    conn.close()


def seed(db):
    db.execute("insert into point_accounts(account_id,engine_key,owner_type,owner_id,created_at,updated_at) values ('a','collateralized_clob_v2','participant','a','x','x')")
    db.execute("insert into collateral_markets(market_id,engine_key,status,point_scale,created_at,updated_at) values ('m','collateralized_clob_v2','open',10000,'x','x')")


def reservation(db, **kw):
    v = dict(engine_key='collateralized_clob_v2',account_id='a',participant_id='a',market_id='m',side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,collateral_type='point',collateral_amount=1000,status='reserved',created_at='x',updated_at='x',contract_version=1,remaining_collateral=1000,consumed_collateral=0,released_collateral=0)
    v.update(kw)
    return db.execute(f"insert into order_collateral_reservations({','.join(v)}) values ({','.join('?' for _ in v)})", tuple(v.values())).lastrowid


def event(db, **kw):
    v = dict(engine_key='collateralized_clob_v2',reservation_id=1,account_id='a',event_type='reserve',asset_type='point',asset_amount=1000,available_before=1000,available_after=0,locked_before=0,locked_after=1000,idempotency_key='key',payload_hash='old-hash',created_at='x')
    v.update(kw)
    return db.execute(f"insert into order_collateral_events({','.join(v)}) values ({','.join('?' for _ in v)})", tuple(v.values())).lastrowid


def test_fixture_schema_and_read_only_validator(db):
    before = db.total_changes
    validate_v2_schema(db)
    assert db.total_changes == before
    assert db.execute('pragma integrity_check').fetchone()[0] == 'ok'
    assert db.execute('pragma foreign_key_check').fetchall() == []
    assert db.execute('pragma user_version').fetchone()[0] == 2
    assert db.execute('select count(*) from v2_priority_sequence').fetchone()[0] == 1
    for table in ('v2_orders','v2_trades','v2_fills','v2_trade_ledger_entries','v2_command_results'):
        assert db.execute(f'select count(*) from {table}').fetchone()[0] == 0
    assert {r[2] for r in db.execute('pragma foreign_key_list(order_collateral_events)')} >= {'order_collateral_reservations','v2_trades','point_accounts'}


@pytest.mark.parametrize('changes', [dict(status='consumed'), dict(status='released',remaining_collateral=0,released_collateral=1000,release_reason=None,released_at='x'), dict(remaining_collateral=999), dict(remaining_collateral=1.5), dict(consumed_collateral=9223372036854775807), dict(contract_version=3), dict(contract_version=2,status='released',remaining_collateral=0,released_collateral=1000,release_reason=None,released_at='x')])
def test_invalid_reservations_rejected(db, changes):
    seed(db)
    with pytest.raises(sqlite3.IntegrityError):
        reservation(db, **changes)


def test_legacy_event_constraints_and_unique(db):
    seed(db); reservation(db); event(db)
    with pytest.raises(sqlite3.IntegrityError): event(db)
    with pytest.raises(sqlite3.IntegrityError): event(db,idempotency_key=None)
    with pytest.raises(sqlite3.IntegrityError): event(db,idempotency_key='other',available_after=1)
    with pytest.raises(sqlite3.IntegrityError): event(db,idempotency_key='other',event_type='release',release_reason=None)
    with pytest.raises(sqlite3.IntegrityError): db.execute('update order_collateral_reservations set collateral_amount=2000,quantity=2,remaining_collateral=2000')


@pytest.mark.parametrize('missing', ['order_collateral_reservations','v2_trades','idx_v2_book_buy'])
def test_missing_schema_rejected(db, missing):
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute(('DROP INDEX ' if missing.startswith('idx_') else 'DROP TABLE ') + missing)
    with pytest.raises(ValueError): validate_v2_schema(db)


def test_malformed_table_ddl_rejected(db):
    db.execute('DROP TABLE v2_priority_sequence')
    db.execute('CREATE TABLE v2_priority_sequence(id INTEGER PRIMARY KEY,last_value INTEGER NOT NULL)')
    db.execute('INSERT INTO v2_priority_sequence VALUES (1,0)')
    with pytest.raises(ValueError, match='schema_invalid_table'): validate_v2_schema(db)


def test_priority_constraints(db):
    with pytest.raises(sqlite3.IntegrityError): db.execute('INSERT INTO v2_priority_sequence VALUES (2,0)')
    with pytest.raises(sqlite3.IntegrityError): db.execute('UPDATE v2_priority_sequence SET last_value=-1')


def test_init_db_contract_rejects_version_two(db):
    with pytest.raises(RuntimeError, match='unsupported schema version: 2'): init_db(db)


def order(db, rid, side, seq, **kw):
    v = dict(reservation_id=rid,engine_key='collateralized_clob_v2',account_id='a',participant_id='a',market_id='m',side=side,outcome='YES',quantity=1,filled_quantity=0,unfilled_quantity=1,fill_count=0,limit_price_micro=1000,status='OPEN',priority_seq=seq,created_at_us=1,updated_at_us=1,version=0)
    v.update(kw)
    return db.execute(f"insert into v2_orders({','.join(v)}) values ({','.join('?' for _ in v)})",tuple(v.values())).lastrowid


def trade(db, **kw):
    v = dict(engine_key='collateralized_clob_v2',market_id='m',outcome='YES',buy_order_id=1,sell_order_id=2,maker_order_id=1,taker_order_id=2,quantity=1,price_micro=1000,execution_key='v2:2:1',taker_fill_ordinal=1,created_at_us=2)
    v.update(kw)
    return db.execute(f"insert into v2_trades({','.join(v)}) values ({','.join('?' for _ in v)})",tuple(v.values())).lastrowid


def test_orders_trades_fills_consume_and_ledger(db):
    seed(db)
    r1 = reservation(db,contract_version=2)
    r2 = reservation(db,contract_version=2,side='SELL',collateral_type='share',collateral_amount=1,remaining_collateral=1)
    order(db,r1,'BUY',1); order(db,r2,'SELL',2)
    with pytest.raises(sqlite3.IntegrityError): order(db,r1,'BUY',3,quantity=0)
    with pytest.raises(sqlite3.IntegrityError): order(db,r1,'BUY',3,status='FILLED')
    with pytest.raises(sqlite3.IntegrityError): trade(db,execution_key='v1:2:1')
    with pytest.raises(sqlite3.IntegrityError): trade(db,taker_fill_ordinal=0)
    tid = trade(db)
    with pytest.raises(sqlite3.IntegrityError): trade(db)
    db.execute("insert into v2_fills(trade_id,order_id,side,quantity,price_micro,created_at_us) values (?,?, 'BUY',1,1000,2)",(tid,1))
    with pytest.raises(sqlite3.IntegrityError): db.execute("insert into v2_fills(trade_id,order_id,side,quantity,price_micro,created_at_us) values (?,?, 'BUY',1,1000,2)",(tid,2))
    consume = dict(event_type='consume',idempotency_key=None,trade_id=tid,event_role='BUY_CONSUME',reservation_before=1000,reservation_after=0,consumed_before=0,consumed_after=1000,available_before=0,available_after=0,locked_before=0,locked_after=0,payload_hash='a'*64)
    event(db,**consume)
    with pytest.raises(sqlite3.IntegrityError): event(db,**consume)
    with pytest.raises(sqlite3.IntegrityError): event(db,**(consume | {'event_role':None}))
    with pytest.raises(sqlite3.IntegrityError): event(db,**(consume | {'event_role':'SELL_CONSUME','idempotency_key':'bad'}))
    sql = "insert into v2_trade_ledger_entries(trade_id,account_id,market_id,outcome,role,asset_type,bucket,delta,balance_before,balance_after,created_at_us) values (?,?,?,?,?,?,?,?,?,?,?)"
    db.execute(sql,(tid,'a','m','YES','BUY_REFUND','point','available',0,0,0,2))
    with pytest.raises(sqlite3.IntegrityError): db.execute(sql,(tid,'a','m','YES','BUY_SHARES','share','available',0,0,0,2))
    with pytest.raises(sqlite3.IntegrityError): db.execute(sql,(tid,'a','m','YES','BUY_REFUND','point','available',0,0,0,2))


@pytest.mark.parametrize('bad', [dict(quantity=0), dict(limit_price_micro=0), dict(limit_price_micro=10001), dict(status='FILLED'), dict(filled_quantity=1,unfilled_quantity=1)])
def test_order_bounds_and_states(db, bad):
    seed(db)
    rid = reservation(db, contract_version=2)
    with pytest.raises(sqlite3.IntegrityError):
        order(db, rid, 'BUY', 1, **bad)


def test_command_unique(db):
    sql = "insert into v2_command_results(engine_key,actor_id,operation,idempotency_key,payload_hash,response_status,response_json,created_at_us) values ('collateralized_clob_v2','a','place','k',?,200,'{}',0)"
    db.execute(sql,('a'*64,))
    with pytest.raises(sqlite3.IntegrityError): db.execute(sql,('b'*64,))


def test_wrong_fk_target_rejected(db):
    db.execute('DROP TABLE v2_fills')
    db.execute(TABLES['v2_fills'].replace('REFERENCES v2_trades(id)', 'REFERENCES v2_orders(id)'))
    db.execute(INDEXES['idx_v2_fills_order'])
    with pytest.raises(ValueError, match='schema_invalid_table:v2_fills'): validate_v2_schema(db)


@pytest.mark.parametrize('table,column', [('markets','payload'), ('demo_users','balance'), ('market_sync_runs','status')])
def test_inherited_v1_required_column_rejected_read_only(db, table, column):
    db.execute(f'ALTER TABLE {table} DROP COLUMN {column}')
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_table:' + table):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_inherited_index_wrong_definition_rejected(db):
    db.execute('DROP INDEX idx_user_sessions_user_id')
    db.execute('CREATE INDEX idx_user_sessions_user_id ON user_sessions(expires_at)')
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_index:idx_user_sessions_user_id'):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_inherited_foreign_key_constraint_rejected(db):
    db.execute('PRAGMA foreign_keys=OFF')
    sql = db.execute("select sql from sqlite_master where type='table' and name='user_sessions'").fetchone()[0]
    assert 'references user_accounts(id)' in sql.lower()
    db.execute('DROP TABLE user_sessions')
    db.execute(sql.replace('references user_accounts(id)', 'references demo_users(user_id)'))
    for name, definition in db.execute("select name,sql from sqlite_master where type='index' and tbl_name='user_sessions' and sql is not null").fetchall():
        db.execute('DROP INDEX "' + name + '"')
    from app.storage import V1_SCHEMA_SQL, _schema_statements
    for stmt in _schema_statements(V1_SCHEMA_SQL):
        if 'create index if not exists idx_user_sessions_' in stmt.lower():
            db.execute(stmt)
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_table:user_sessions'):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_v2_ddl_whitespace_variation_is_accepted(db):
    original = TABLES['v2_priority_sequence']
    db.execute('DROP TABLE v2_priority_sequence')
    db.execute(original.replace('(id=', '( id = ').replace(', last_value', ' ,  last_value'))
    db.execute('INSERT INTO v2_priority_sequence VALUES (1,0)')
    db.commit()
    before = list(db.iterdump())
    validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_legacy_reordered_columns_migrate_and_validate():
    from app.v2_clob_migration import IsolatedMigrationFixture, migrate_fixture
    with IsolatedMigrationFixture() as fixture:
        c = fixture.conn
        c.execute('PRAGMA foreign_keys=OFF')
        c.execute('DROP TABLE markets')
        c.execute('CREATE TABLE markets(updated_at TEXT NOT NULL, payload TEXT NOT NULL, market_id TEXT PRIMARY KEY)')
        c.commit()
        c.execute('PRAGMA foreign_keys=ON')
        assert migrate_fixture(fixture)
        before = list(c.iterdump())
        validate_v2_schema(c)
        assert list(c.iterdump()) == before


def test_inherited_type_and_notnull_constraints_rejected(db):
    db.execute('PRAGMA foreign_keys=OFF')
    db.execute('DROP TABLE markets')
    db.execute('CREATE TABLE markets(market_id TEXT PRIMARY KEY, payload BLOB, updated_at TEXT NOT NULL)')
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_table:markets'):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_inherited_unexpected_unique_expression_fails_closed(db):
    db.execute('CREATE UNIQUE INDEX unique_market_expression ON markets(lower(payload))')
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_table:markets'):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_inherited_check_constraint_removed_is_rejected(db):
    db.execute('PRAGMA foreign_keys=OFF')
    sql = db.execute("select sql from sqlite_master where type='table' and name='point_accounts'").fetchone()[0]
    indexes = [row[0] for row in db.execute("select sql from sqlite_master where type='index' and tbl_name='point_accounts' and sql is not null")]
    assert 'check(available_micro >= 0)' in sql
    db.execute('DROP TABLE point_accounts')
    db.execute(sql.replace('check(available_micro >= 0)', ''))
    for statement in indexes:
        db.execute(statement)
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_table:point_accounts'):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_v2_missing_column_rejected_without_mutation(db):
    db.execute('ALTER TABLE v2_command_results DROP COLUMN response_json')
    db.commit()
    before = list(db.iterdump())
    with pytest.raises(ValueError, match='schema_invalid_table:v2_command_results'):
        validate_v2_schema(db)
    assert list(db.iterdump()) == before


def test_declared_v2_with_only_v1_schema_is_rejected():
    c = connect(':memory:')
    try:
        init_db(c)
        c.execute('PRAGMA user_version=2')
        before = list(c.iterdump())
        with pytest.raises(ValueError):
            validate_v2_schema(c)
        assert list(c.iterdump()) == before
    finally:
        c.close()
