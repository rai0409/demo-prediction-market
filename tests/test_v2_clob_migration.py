import sqlite3

import pytest

from app.collateral_ledger import (CollateralLedgerError, POINT_SCALE, allocate_v2_points_to_participant,
    bootstrap_v2_point_supply, cancel_v2_order_collateral, create_collateral_market,
    reject_v2_order_collateral, reserve_v2_order_collateral, split_complete_sets,
    verify_collateral_invariants)
from app.storage import CURRENT_SCHEMA_COLUMNS, DEMO_USER_ID, store_markets, verify_audit_chain
from app.v2_clob_migration import IsolatedMigrationFixture, migrate_fixture
from app.v2_clob_schema import validate_v2_schema


@pytest.fixture
def fixture():
    with IsolatedMigrationFixture() as f:
        yield f


def populated(f, markets):
    c = f.conn
    store_markets(c, markets)
    c.commit()
    bootstrap_v2_point_supply(c, amount_micro=10*POINT_SCALE, idempotency_key='bootstrap')
    allocation = allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=6*POINT_SCALE,idempotency_key='fund')
    market = markets[0]['market_id']
    create_collateral_market(c,market_id=market)
    split_complete_sets(c,account_id=allocation['destination_account_id'],market_id=market,quantity=2,idempotency_key='split')
    buy = reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='buy')
    sell = reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='SELL',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='sell')
    cancelled = reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='NO',quantity=1,limit_price_micro=1000,idempotency_key='cancel-me')
    cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=cancelled['reservation_id'],idempotency_key='cancel')
    return market, buy, sell


def snapshot(c):
    names=('order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries')
    tables = [row[0] for row in c.execute("select name from sqlite_master where type='table' and name not like 'sqlite_%'")]
    all_rows = {name: [tuple(row) for row in c.execute('select * from "' + name.replace('"', '""') + '"')] for name in tables}
    return ({n:[tuple(r) for r in c.execute('select * from '+n+' order by id')] for n in names},
        [tuple(r) for r in c.execute('select * from sqlite_sequence order by name')],
        [tuple(r) for r in c.execute("select type,name,sql from sqlite_master where sql is not null order by type,name")],
        [tuple(r) for r in c.execute('select * from demo_audit_events order by id')], all_rows)


def test_populated_migration_and_legacy_writer(fixture, sample_markets):
    c = fixture.conn
    market, buy, sell = populated(fixture,sample_markets)
    before=snapshot(c)
    assert migrate_fixture(fixture)
    validate_v2_schema(c)
    assert c.execute('pragma user_version').fetchone()[0]==2
    assert c.execute('pragma foreign_key_check').fetchall()==[]
    assert verify_audit_chain(c)['integrity_status']=='verified'
    assert [tuple(r)[:17] for r in c.execute('select * from order_collateral_reservations order by id')] == before[0]['order_collateral_reservations']
    assert [tuple(r)[:16] for r in c.execute('select * from order_collateral_events order by id')] == before[0]['order_collateral_events']
    assert [tuple(r) for r in c.execute('select * from order_collateral_ledger_entries order by id')] == before[0]['order_collateral_ledger_entries']
    assert [tuple(r) for r in c.execute('select * from demo_audit_events order by id')]==before[3]
    before_replay = snapshot(c)
    changes_before = c.total_changes
    assert not migrate_fixture(fixture)
    assert snapshot(c) == before_replay
    assert c.total_changes == changes_before
    cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=buy['reservation_id'],idempotency_key='cancel-buy')
    reject_v2_order_collateral(c,reservation_id=sell['reservation_id'],idempotency_key='reject-sell')
    assert verify_collateral_invariants(c)['integrity_status']=='verified'
    new=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='new')
    assert set(new) == set(buy)
    assert reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='new')['idempotent_replay']
    with pytest.raises(CollateralLedgerError, match='idempotency_conflict'):
        reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=999,idempotency_key='new')
    assert new['reservation_id']>buy['reservation_id']
    sell_new = reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='SELL',outcome='YES',quantity=1,limit_price_micro=900,idempotency_key='new-sell')
    assert reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='SELL',outcome='YES',quantity=1,limit_price_micro=900,idempotency_key='new-sell')['idempotent_replay']
    cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=sell_new['reservation_id'],idempotency_key='cancel-new-sell')
    row = c.execute('select contract_version,remaining_collateral,consumed_collateral,released_collateral from order_collateral_reservations where id=?',(sell_new['reservation_id'],)).fetchone()
    assert tuple(row) == (1,0,0,1)
    assert verify_audit_chain(c)['integrity_status']=='verified'


def test_old_release_refuses_contract_two_and_rolls_back(fixture, sample_markets):
    c=fixture.conn
    _, buy, _=populated(fixture,sample_markets)
    migrate_fixture(fixture)
    c.execute("""insert into order_collateral_reservations(engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,status,created_at,updated_at,contract_version,remaining_collateral,consumed_collateral,released_collateral)
        select engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,status,created_at,updated_at,2,remaining_collateral,consumed_collateral,released_collateral from order_collateral_reservations where id=?""",(buy['reservation_id'],))
    new_id=c.execute('select last_insert_rowid()').fetchone()[0]
    c.commit()
    before=[tuple(row) for row in c.execute('select * from point_accounts order by account_id')]
    with pytest.raises(CollateralLedgerError,match='unsupported_reservation_contract'):
        cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=new_id,idempotency_key='reject-v2')
    assert [tuple(row) for row in c.execute('select * from point_accounts order by account_id')]==before
    assert c.execute('select status from order_collateral_reservations where id=?',(new_id,)).fetchone()[0]=='reserved'


@pytest.mark.parametrize('stage',['shadows','copy_order_collateral_reservations','copy_order_collateral_events','copy_order_collateral_ledger_entries','drop_old','rename','indexes','precommit'])
def test_failure_rolls_back(fixture,sample_markets,stage):
    populated(fixture,sample_markets)
    before=snapshot(fixture.conn)
    def fail(current):
        if current==stage: raise RuntimeError('injected')
    with pytest.raises(RuntimeError,match='injected'):
        migrate_fixture(fixture,failure_hook=fail)
    assert fixture.conn.execute('pragma user_version').fetchone()[0]==1
    assert fixture.conn.execute('pragma foreign_keys').fetchone()[0]==1
    assert snapshot(fixture.conn)==before


def test_autoincrement_high_water_survives_deleted_ids(fixture, sample_markets):
    c=fixture.conn
    market, _, _ = populated(fixture, sample_markets)
    for name in ('order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries'):
        columns = CURRENT_SCHEMA_COLUMNS[name]
        values = ['1000' if col == 'id' else "'temporary-key'" if col == 'idempotency_key' else '1000' if name == 'order_collateral_ledger_entries' and col == 'event_id' else col for col in columns]
        c.execute(f"insert into {name} ({','.join(columns)}) select {','.join(values)} from {name} limit 1")
    for name in ('order_collateral_ledger_entries','order_collateral_events','order_collateral_reservations'):
        c.execute(f'delete from {name} where id=1000')
    c.commit()
    assert migrate_fixture(fixture)
    current=dict(c.execute("select name,seq from sqlite_sequence where name like 'order_collateral_%'").fetchall())
    assert all(current[n]>=1000 for n in ('order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries'))
    new=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='NO',quantity=1,limit_price_micro=500,idempotency_key='after-migration')
    assert new['reservation_id']>1000
    assert c.execute('select max(id) from order_collateral_events').fetchone()[0]>1000
    assert c.execute('select min(id) from order_collateral_ledger_entries where id>1000').fetchone()[0]>1000


def test_failed_migration_can_be_retried(fixture, sample_markets):
    populated(fixture,sample_markets)
    def fail(stage):
        if stage=='precommit': raise RuntimeError('injected')
    with pytest.raises(RuntimeError,match='injected'): migrate_fixture(fixture,failure_hook=fail)
    assert migrate_fixture(fixture)
    assert migrate_fixture(fixture) is False


def test_caller_transaction_and_wrong_version_fail_closed(fixture):
    c=fixture.conn
    c.execute("insert into demo_users values ('pending',0)")
    with pytest.raises(RuntimeError,match='clean connection'): migrate_fixture(fixture)
    assert c.in_transaction
    c.rollback()
    c.execute('pragma user_version=3')
    with pytest.raises(RuntimeError,match='unsupported schema version'): migrate_fixture(fixture)
    assert c.execute('pragma user_version').fetchone()[0]==3


def test_incomplete_v2_rejected(fixture):
    assert migrate_fixture(fixture)
    fixture.conn.execute('drop index idx_v2_book_buy')
    fixture.conn.commit()
    with pytest.raises(ValueError): migrate_fixture(fixture)


def test_broken_foreign_key_rejected_before_migration(fixture):
    c=fixture.conn
    c.execute('pragma foreign_keys=OFF')
    c.execute("insert into order_collateral_reservations(engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,status,created_at,updated_at) values ('collateralized_clob_v2','missing','p','missing','BUY','YES',1,1,'point',1,'reserved','x','x')")
    c.commit()
    c.execute('pragma foreign_keys=ON')
    before=snapshot(c)
    with pytest.raises(RuntimeError): migrate_fixture(fixture)
    assert c.execute('pragma user_version').fetchone()[0]==1
    assert snapshot(c)==before


def test_invalid_legacy_row_and_schema_rejected(fixture, sample_markets):
    populated(fixture,sample_markets)
    c=fixture.conn
    c.execute('pragma ignore_check_constraints=ON')
    c.execute("update order_collateral_reservations set status='invalid' where id=1")
    c.commit()
    c.execute('pragma ignore_check_constraints=OFF')
    before=snapshot(c)
    with pytest.raises(RuntimeError): migrate_fixture(fixture)
    assert c.execute('pragma user_version').fetchone()[0]==1
    assert snapshot(c)==before


def test_unexpected_index_is_not_discarded(fixture):
    c=fixture.conn
    c.execute('create index custom_reservation_index on order_collateral_reservations(status)')
    c.commit()
    before=snapshot(c)
    with pytest.raises(RuntimeError,match='legacy_schema_invalid'): migrate_fixture(fixture)
    assert snapshot(c)==before


@pytest.mark.parametrize('stage', ['indexes', 'precommit'])
def test_accounting_violation_rolls_back_before_commit(fixture, sample_markets, stage):
    populated(fixture, sample_markets)
    c = fixture.conn
    before = snapshot(c)
    observed = []

    def corrupt(current):
        if current == stage:
            assert c.in_transaction
            c.execute("update point_accounts set available_micro=available_micro+1 where account_id=(select account_id from point_accounts limit 1)")
            observed.append(current)

    with pytest.raises(RuntimeError, match='^collateral_invalid$'):
        migrate_fixture(fixture, failure_hook=corrupt)
    assert observed == [stage]
    assert not c.in_transaction
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert c.execute('pragma foreign_keys').fetchone()[0] == 1
    assert c.execute('pragma integrity_check').fetchone()[0] == 'ok'
    assert c.execute('pragma foreign_key_check').fetchall() == []
    assert snapshot(c) == before
    assert verify_audit_chain(c)['integrity_status'] == 'verified'
    assert verify_collateral_invariants(c)['integrity_status'] == 'verified'
    assert migrate_fixture(fixture) is True
    assert migrate_fixture(fixture) is False


def test_sequence_loss_rolls_back_before_commit(fixture, sample_markets):
    populated(fixture, sample_markets)
    c = fixture.conn
    before = snapshot(c)

    def corrupt(stage):
        if stage == 'precommit':
            c.execute("delete from sqlite_sequence where name='order_collateral_reservations'")

    with pytest.raises(RuntimeError, match='^legacy_sequence_changed$'):
        migrate_fixture(fixture, failure_hook=corrupt)
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert c.execute('pragma foreign_keys').fetchone()[0] == 1
    assert snapshot(c) == before
    assert migrate_fixture(fixture) is True


def test_preflight_rejects_unregistered_fixture(fixture):
    from app.v2_clob_migration import IsolatedMigrationFixture
    fake = object.__new__(IsolatedMigrationFixture)
    with pytest.raises(RuntimeError, match='fixture_ownership_invalid'):
        migrate_fixture(fake)


def test_preflight_rejects_replaced_connection(fixture):
    c = fixture.conn
    before = snapshot(c)
    fixture.conn = sqlite3.connect(':memory:')
    try:
        with pytest.raises(RuntimeError, match='fixture_ownership_invalid'):
            migrate_fixture(fixture)
    finally:
        fixture.conn.close()
        fixture.conn = c
    assert snapshot(c) == before
    assert c.execute('pragma user_version').fetchone()[0] == 1


@pytest.mark.parametrize('mode', ['insufficient', 'unavailable'])
def test_capacity_preflight_rejects_without_changes(fixture, sample_markets, monkeypatch, mode):
    import app.v2_clob_migration as migration
    populated(fixture, sample_markets)
    c = fixture.conn
    before = snapshot(c)
    def disk_usage(path):
        if mode == 'unavailable':
            raise OSError('injected')
        return type('Usage', (), {'free': 0})()
    monkeypatch.setattr(migration.shutil, 'disk_usage', disk_usage)
    with pytest.raises(RuntimeError, match='capacity_' + mode):
        migrate_fixture(fixture)
    assert snapshot(c) == before
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert not c.in_transaction


def test_active_writer_rejected_before_ddl(fixture, sample_markets):
    populated(fixture, sample_markets)
    c = fixture.conn
    c.execute('pragma busy_timeout=10')
    before = snapshot(c)
    other = sqlite3.connect(fixture.path, timeout=0.01)
    try:
        other.execute('BEGIN IMMEDIATE')
        with pytest.raises(sqlite3.OperationalError, match='locked'):
            migrate_fixture(fixture)
        assert snapshot(c) == before
        assert c.execute('pragma user_version').fetchone()[0] == 1
    finally:
        other.rollback()
        other.close()


def test_writer_blocked_during_migration(fixture, sample_markets):
    populated(fixture, sample_markets)
    other = sqlite3.connect(fixture.path, timeout=0.01)
    attempted = []
    def probe(stage):
        if stage == 'precommit':
            with pytest.raises(sqlite3.OperationalError, match='locked'):
                other.execute('BEGIN IMMEDIATE')
            attempted.append(stage)
    try:
        assert migrate_fixture(fixture, failure_hook=probe)
        assert attempted == ['precommit']
    finally:
        other.close()


@pytest.mark.parametrize('code', [sqlite3.SQLITE_FULL, sqlite3.SQLITE_IOERR])
def test_io_failure_rolls_back(fixture, sample_markets, code):
    populated(fixture, sample_markets)
    c = fixture.conn
    before = snapshot(c)
    def fail(stage):
        if stage == 'rename':
            error = sqlite3.OperationalError('injected storage failure')
            error.sqlite_errorcode = code
            raise error
    with pytest.raises(sqlite3.OperationalError, match='injected storage failure') as error:
        migrate_fixture(fixture, failure_hook=fail)
    assert error.value.sqlite_errorcode == code
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert c.execute('pragma foreign_keys').fetchone()[0] == 1
    assert snapshot(c) == before
    assert migrate_fixture(fixture)


def test_sqlite_auto_rollback_preserves_original_error(fixture, sample_markets):
    populated(fixture, sample_markets)
    c = fixture.conn
    before = snapshot(c)
    def fail(stage):
        if stage == 'rename':
            # Model SQLite having already aborted the transaction on I/O failure.
            c.rollback()
            error = sqlite3.OperationalError('injected auto rollback')
            error.sqlite_errorcode = sqlite3.SQLITE_IOERR
            raise error
    with pytest.raises(sqlite3.OperationalError, match='injected auto rollback') as error:
        migrate_fixture(fixture, failure_hook=fail)
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_IOERR
    assert not c.in_transaction
    assert c.execute('pragma foreign_keys').fetchone()[0] == 1
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert snapshot(c) == before
    assert migrate_fixture(fixture)


def test_sqlite_actual_full_preserves_v1(fixture, sample_markets):
    populated(fixture, sample_markets)
    c = fixture.conn
    before = snapshot(c)
    pages = c.execute('pragma page_count').fetchone()[0]
    c.execute(f'pragma max_page_count={pages}')
    with pytest.raises(sqlite3.OperationalError) as error:
        migrate_fixture(fixture)
    assert error.value.sqlite_errorcode == sqlite3.SQLITE_FULL
    assert not c.in_transaction
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert c.execute('pragma foreign_keys').fetchone()[0] == 1
    assert snapshot(c) == before


def test_unsafe_journal_mode_rejected_before_ddl(fixture, sample_markets):
    populated(fixture, sample_markets)
    c = fixture.conn
    assert c.execute('pragma journal_mode=off').fetchone()[0] == 'off'
    before = snapshot(c)
    with pytest.raises(RuntimeError, match='fixture_journal_mode_invalid'):
        migrate_fixture(fixture)
    assert c.execute('pragma user_version').fetchone()[0] == 1
    assert snapshot(c) == before
