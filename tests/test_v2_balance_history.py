import pytest

from app.v2_balance_history import HistoryError, install_checkpoint
from app.v2_clob_migration import IsolatedMigrationFixture, migrate_fixture
from app.collateral_ledger import bootstrap_v2_point_supply


def test_checkpoint_and_rollback():
    with IsolatedMigrationFixture() as f:
        bootstrap_v2_point_supply(f.conn, amount_micro=1000, idempotency_key='bootstrap')
        migrate_fixture(f)
        before = [tuple(x) for x in f.conn.execute('select * from point_accounts')]
        f.conn.execute('begin')
        install_checkpoint(f.conn)
        assert f.conn.execute('select count(*) from balance_history_bucket').fetchone()[0] == 2
        with pytest.raises(Exception, match='balance_history_source_protocol_unavailable'):
            f.conn.execute('update point_accounts set available_micro=available_micro+1')
        assert [tuple(x) for x in f.conn.execute('select * from point_accounts')] == before
        f.conn.rollback()
        assert f.conn.execute("select count(*) from sqlite_master where name like 'balance_history_%'").fetchone()[0] == 0


def test_empty_checkpoint_requires_caller_transaction():
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        with pytest.raises(HistoryError, match='transaction_required'):
            install_checkpoint(f.conn)
        f.conn.execute('begin')
        install_checkpoint(f.conn)
        assert f.conn.execute('select checkpoint_count from balance_history_generation').fetchone()[0] == 0
        f.conn.commit()
        f.conn.execute('begin')
        with pytest.raises(HistoryError, match='existing_history_requires_validation'):
            install_checkpoint(f.conn)
        f.conn.rollback()


def test_all_five_checkpoint_kinds(sample_markets):
    from test_v2_clob_migration import populated
    with IsolatedMigrationFixture() as f:
        populated(f, sample_markets)
        migrate_fixture(f)
        c = f.conn
        original = {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)]
                    for table in ('point_accounts', 'outcome_positions', 'market_reserves', 'demo_audit_events')}
        c.execute('BEGIN')
        install_checkpoint(c)
        actual = {tuple(r) for r in c.execute('SELECT kind,account_id,market_id,outcome,opening_value FROM balance_history_bucket')}
        expected = set()
        for r in c.execute('SELECT account_id,available_micro,locked_micro FROM point_accounts'):
            expected.update((('POINT_AVAILABLE',r[0],None,None,r[1]), ('POINT_LOCKED',r[0],None,None,r[2])))
        for r in c.execute('SELECT account_id,market_id,outcome,available_shares,locked_shares FROM outcome_positions'):
            expected.update((('SHARE_AVAILABLE',r[0],r[1],r[2],r[3]), ('SHARE_LOCKED',r[0],r[1],r[2],r[4])))
        for r in c.execute('SELECT market_id,reserve_micro FROM market_reserves'):
            expected.add(('MARKET_RESERVE',None,r[0],None,r[1]))
        assert actual == expected
        assert {r[0] for r in actual} == {'POINT_AVAILABLE','POINT_LOCKED','SHARE_AVAILABLE','SHARE_LOCKED','MARKET_RESERVE'}
        c.commit()
        assert original == {table: [tuple(r) for r in c.execute('SELECT * FROM ' + table)] for table in original}


def test_unresolved_context_cannot_commit_and_savepoint_rolls_back():
    import sqlite3
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c = f.conn
        c.execute('BEGIN')
        install_checkpoint(c)
        c.commit()
        c.execute('BEGIN')
        c.execute('SAVEPOINT context_step')
        c.execute("INSERT INTO balance_history_context(operation_id,guard) VALUES ('unfinalized',1)")
        with pytest.raises(sqlite3.IntegrityError):
            c.commit()
        assert c.in_transaction
        c.execute('ROLLBACK TO context_step')
        c.execute('RELEASE context_step')
        c.commit()
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0] == 0
        with pytest.raises(sqlite3.IntegrityError, match='balance_history_sentinel_immutable'):
            c.execute('INSERT INTO balance_history_context_must_be_empty VALUES (1)')


def test_bootstrap_source_evidence_is_validated_without_mutation():
    from app.v2_balance_history import validate_bootstrap_source
    with IsolatedMigrationFixture() as f:
        result = bootstrap_v2_point_supply(f.conn, amount_micro=1000, idempotency_key='source-check')
        migrate_fixture(f)
        c = f.conn
        before = c.total_changes
        args = dict(source_id=result['event_id'], account_id=result['destination_account_id'], before=0, after=1000)
        validate_bootstrap_source(c, **args)
        assert c.total_changes == before
        with pytest.raises(HistoryError, match='invalid_bootstrap_source'):
            validate_bootstrap_source(c, **{**args, 'source_id': result['event_id'] + 1})
        with pytest.raises(HistoryError, match='invalid_bootstrap_source'):
            validate_bootstrap_source(c, **{**args, 'after': 1001})
        c.execute('BEGIN')
        c.execute("UPDATE collateral_ledger_entries SET account_available_after_micro=1001 WHERE reference_type='point_supply_event' AND reference_id=?", (str(result['event_id']),))
        with pytest.raises(HistoryError, match='invalid_bootstrap_ledger'):
            validate_bootstrap_source(c, **args)
        c.rollback()


def test_bootstrap_history_commit_and_reopen():
    import sqlite3
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN')
        install_checkpoint(c)
        c.commit()
        result=bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='history-bootstrap')
        assert not c.in_transaction
        row=c.execute('SELECT e.balance_before,e.delta,e.balance_after,o.source_id FROM balance_history_event e JOIN balance_history_operation o USING(operation_id)').fetchone()
        assert tuple(row)==(0,1000,1000,result['event_id'])
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        with sqlite3.connect(f.path) as other:
            assert other.execute('SELECT available_micro FROM point_accounts WHERE account_id=?',(result['destination_account_id'],)).fetchone()[0]==1000
            assert tuple(other.execute('SELECT balance_before,delta,balance_after FROM balance_history_event').fetchone())==(0,1000,1000)
        replay=bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='history-bootstrap')
        assert replay['idempotent_replay']
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==1
        with pytest.raises(sqlite3.IntegrityError, match='balance_history_source_protocol_unavailable'):
            c.execute('UPDATE point_accounts SET available_micro=available_micro+1')


def test_bootstrap_outer_transaction_rollback_and_savepoint():
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        c.execute('BEGIN')
        result=bootstrap_v2_point_supply(c,amount_micro=900,idempotency_key='outer')
        assert c.in_transaction
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==1
        c.rollback()
        for table in ('point_accounts','point_supply_events','balance_history_event','balance_history_operation','balance_history_context'):
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        assert bootstrap_v2_point_supply(c,amount_micro=900,idempotency_key='outer')['event_id']==result['event_id']


def test_bootstrap_failure_is_atomic(monkeypatch):
    import app.v2_balance_history as history
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        def fail(**kwargs):
            raise AssertionError('injected')
        monkeypatch.setattr(history,'validate_bootstrap_source',lambda *a,**kw: fail())
        with pytest.raises(AssertionError,match='injected'):
            bootstrap_v2_point_supply(c,amount_micro=900,idempotency_key='failure')
        assert not c.in_transaction
        for table in ('point_accounts','point_supply_events','balance_history_event','balance_history_operation','balance_history_context'):
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0


def test_history_rejects_foreign_keys_disabled_and_replay_conflict():
    import sqlite3
    from app.collateral_ledger import CollateralLedgerError
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        c.execute('PRAGMA foreign_keys=OFF')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_foreign_keys_required'):
            c.execute("INSERT INTO balance_history_context(operation_id,guard) VALUES ('bad',1)")
        c.rollback(); c.execute('PRAGMA foreign_keys=ON')
        bootstrap_v2_point_supply(c,amount_micro=900,idempotency_key='same')
        with pytest.raises(CollateralLedgerError,match='idempotency_conflict'):
            bootstrap_v2_point_supply(c,amount_micro=901,idempotency_key='same')
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==1


def test_invalid_source_cannot_authorize_or_commit():
    import sqlite3
    from app.v2_balance_history import begin_bootstrap
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        c.execute('BEGIN')
        begin_bootstrap(c,source_id=9999,account_id='unknown',amount=10)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("INSERT INTO point_accounts(account_id,engine_key,owner_type,owner_id,created_at,updated_at) VALUES ('unknown','collateralized_clob_v2','operator','operator-treasury','t','t')")
        with pytest.raises(sqlite3.IntegrityError):
            c.commit()
        c.rollback()
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==0


def test_context_not_reusable_from_another_connection():
    import sqlite3
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=750,idempotency_key='once')
        with sqlite3.connect(f.path) as other:
            other.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
                other.execute('UPDATE point_accounts SET available_micro=available_micro+1')
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==1


def test_finalized_bootstrap_evidence_is_immutable():
    import sqlite3
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=550,idempotency_key='immutable')
        for sql in ("UPDATE point_supply_events SET amount_micro=551", "DELETE FROM collateral_ledger_entries", "DELETE FROM demo_audit_events WHERE event_type='v2_point_supply_bootstrapped'"):
            with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_immutable'):
                c.execute(sql)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_immutable'):
            c.execute("INSERT INTO demo_audit_events(event_type,reference_type,reference_id) VALUES ('duplicate','point_supply_event','1')")
        c.rollback()


def test_unfinalized_context_and_unregistered_source_are_rejected():
    import sqlite3
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("INSERT INTO point_supply_events(engine_key,event_type,destination_account_id,amount_micro,idempotency_key,payload_hash,created_at) VALUES ('collateralized_clob_v2','bootstrap_issue','bad',1,'bad','bad','t')")
        c.rollback()
        c.execute('BEGIN')
        c.execute("INSERT INTO balance_history_context(operation_id,guard) VALUES ('pending',1)")
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_operation_unfinalized'):
            c.execute("DELETE FROM balance_history_context WHERE operation_id='pending'")
        with pytest.raises(sqlite3.IntegrityError):
            c.commit()
        c.rollback()


def test_bootstrap_failure_rolls_back_only_nested_savepoint(monkeypatch):
    import app.v2_balance_history as history
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        c.execute('BEGIN')
        c.execute('CREATE TABLE caller_marker(value INTEGER)')
        monkeypatch.setattr(history,'validate_bootstrap_source',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('injected')))
        with pytest.raises(RuntimeError,match='injected'):
            bootstrap_v2_point_supply(c,amount_micro=90,idempotency_key='savepoint-fail')
        assert c.in_transaction
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='caller_marker'").fetchone()[0]==1
        for table in ('point_accounts','point_supply_events','balance_history_context','balance_history_event','balance_history_operation'):
            assert c.execute('SELECT count(*) FROM '+table).fetchone()[0]==0
        c.rollback()


def test_history_allocation_new_existing_and_reopen():
    import sqlite3
    from app.collateral_ledger import allocate_v2_points_to_participant
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f)
        c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=10000,idempotency_key='bootstrap-allocation')
        result=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=3000,idempotency_key='allocation-a')
        rows=c.execute('''SELECT b.account_id,e.role,e.balance_before,e.delta,e.balance_after
          FROM balance_history_event e JOIN balance_history_bucket b USING(bucket_id)
          JOIN balance_history_operation o USING(operation_id)
          WHERE o.source_type='point_allocation_event' AND o.source_id=? ORDER BY e.event_seq''',(result['event_id'],)).fetchall()
        treasury_id=c.execute('SELECT source_account_id FROM point_allocation_events WHERE id=?',(result['event_id'],)).fetchone()[0]
        assert [tuple(r) for r in rows]==[(treasury_id,'ALLOCATION_DEBIT',10000,-3000,7000),(result['destination_account_id'],'ALLOCATION_CREDIT',0,3000,3000)]
        with sqlite3.connect(f.path) as other:
            assert other.execute('SELECT available_micro FROM point_accounts WHERE account_id=?',(result['destination_account_id'],)).fetchone()[0]==3000
            assert other.execute("SELECT count(*) FROM balance_history_event WHERE role IN ('ALLOCATION_DEBIT','ALLOCATION_CREDIT')").fetchone()[0]==2
            assert other.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
            other.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
                other.execute('UPDATE point_accounts SET available_micro=available_micro+1 WHERE account_id=?',(result['destination_account_id'],))
        second=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=500,idempotency_key='allocation-b')
        assert second['destination_account_id']==result['destination_account_id']
        assert c.execute("SELECT count(*) FROM balance_history_bucket WHERE account_id=?",(result['destination_account_id'],)).fetchone()[0]==2
        assert allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=500,idempotency_key='allocation-b')['idempotent_replay']
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role IN ('ALLOCATION_DEBIT','ALLOCATION_CREDIT')").fetchone()[0]==4
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]


def test_history_allocation_failure_and_idempotency(monkeypatch):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f); c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='bootstrap-failure')
        with pytest.raises(ledger.CollateralLedgerError,match='insufficient_points'):
            ledger.allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=1001,idempotency_key='insufficient')
        assert c.execute("SELECT count(*) FROM point_allocation_events").fetchone()[0]==0
        original=ledger._record_ledger
        def skip_credit(conn,**kwargs):
            if kwargs['entry_type']!='participant_allocation_credit': original(conn,**kwargs)
        monkeypatch.setattr(ledger,'_record_ledger',skip_credit)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            ledger.allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=200,idempotency_key='bad-ledger')
        assert c.execute("SELECT count(*) FROM point_allocation_events").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ALLOCATION_%'").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM point_accounts WHERE owner_type='participant'").fetchone()[0]==0
        assert c.execute("SELECT available_micro FROM point_accounts WHERE owner_type='operator'").fetchone()[0]==1000
        monkeypatch.setattr(ledger,'_record_ledger',original)
        ledger.allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=200,idempotency_key='same')
        with pytest.raises(ledger.CollateralLedgerError,match='idempotency_conflict'):
            ledger.allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=201,idempotency_key='same')
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ALLOCATION_%'").fetchone()[0]==2


def test_history_allocation_nested_rollback_and_direct_update():
    import sqlite3
    from app.collateral_ledger import allocate_v2_points_to_participant
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f); c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='bootstrap-rollback')
        c.execute('BEGIN')
        allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=200,idempotency_key='rollback')
        assert c.in_transaction
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ALLOCATION_%'").fetchone()[0]==2
        c.rollback()
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ALLOCATION_%'").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM point_allocation_events").fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM balance_history_context").fetchone()[0]==0
        assert c.execute("SELECT available_micro FROM point_accounts WHERE owner_type='operator'").fetchone()[0]==1000
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("UPDATE point_accounts SET available_micro=available_micro-1")


def test_history_allocation_rejects_missing_event_and_wrong_role():
    import sqlite3
    from app.collateral_ledger import allocate_v2_points_to_participant
    from app.storage import DEMO_USER_ID
    from app.v2_balance_history import begin_allocation
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f); c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='bootstrap-partial')
        first=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=100,idempotency_key='initial')
        treasury=c.execute("SELECT account_id FROM point_accounts WHERE owner_type='operator'").fetchone()[0]
        destination=first['destination_account_id']
        c.execute('BEGIN')
        op=begin_allocation(c,account_id=treasury,destination_id=destination,participant_id=DEMO_USER_ID,amount=50)
        cur=c.execute("""INSERT INTO point_allocation_events(engine_key,source_account_id,destination_account_id,participant_id,amount_micro,source_before_micro,source_after_micro,destination_before_micro,destination_after_micro,idempotency_key,payload_hash,created_at) VALUES ('collateralized_clob_v2',?,?,?,50,900,850,100,150,'partial','hash','t')""",(treasury,destination,DEMO_USER_ID))
        bucket=c.execute("SELECT generation_id,bucket_id FROM balance_history_bucket WHERE account_id=? AND kind='POINT_AVAILABLE'",(treasury,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_event_invalid'):
            c.execute("INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after) VALUES (?,?,?,'ALLOCATION_CREDIT',900,50,950)",(bucket[0],bucket[1],op))
        c.execute('UPDATE point_accounts SET available_micro=850 WHERE account_id=?',(treasury,))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            c.execute("INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,amount,payload_hash) VALUES (?,'point_allocation_event',?,?,50,'hash')",(op,cur.lastrowid,treasury))
        with pytest.raises(sqlite3.IntegrityError): c.commit()
        c.rollback()
        assert c.execute('SELECT available_micro FROM point_accounts WHERE account_id=?',(treasury,)).fetchone()[0]==900
        assert c.execute("SELECT count(*) FROM balance_history_context").fetchone()[0]==0


def test_history_allocation_failure_preserves_caller_savepoint(monkeypatch):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f); c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='bootstrap-nested-allocation')
        c.execute('BEGIN'); c.execute('CREATE TABLE caller_marker(value INTEGER)')
        original=ledger._record_ledger
        def skip_credit(conn,**kwargs):
            if kwargs['entry_type']!='participant_allocation_credit': original(conn,**kwargs)
        monkeypatch.setattr(ledger,'_record_ledger',skip_credit)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            ledger.allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=200,idempotency_key='nested-failure')
        assert c.in_transaction
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='caller_marker'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM point_allocation_events').fetchone()[0]==0
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ALLOCATION_%'").fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        c.rollback()


def test_history_allocation_rejects_invalid_source_and_overflow():
    import sqlite3
    from app.collateral_ledger import CollateralLedgerError, allocate_v2_points_to_participant
    from app.storage import DEMO_USER_ID
    from app.v2_balance_history import begin_allocation
    with IsolatedMigrationFixture() as f:
        migrate_fixture(f); c=f.conn
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=1000,idempotency_key='bootstrap-invalid')
        first=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=100,idempotency_key='initial-invalid')
        with pytest.raises(CollateralLedgerError,match='integer_overflow'):
            allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=9223372036854775808,idempotency_key='overflow')
        treasury=c.execute("SELECT account_id FROM point_accounts WHERE owner_type='operator'").fetchone()[0]
        c.execute('BEGIN')
        begin_allocation(c,account_id=treasury,destination_id=first['destination_account_id'],participant_id=DEMO_USER_ID,amount=50)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("""INSERT INTO point_allocation_events(engine_key,source_account_id,destination_account_id,participant_id,amount_micro,source_before_micro,source_after_micro,destination_before_micro,destination_after_micro,idempotency_key,payload_hash,created_at) VALUES ('collateralized_clob_v2',?,?,?,40,900,860,100,140,'wrong','hash','t')""",(treasury,first['destination_account_id'],DEMO_USER_ID))
        c.rollback()
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0


def test_history_market_split_merge_round_trip(sample_markets):
    import sqlite3
    from app.collateral_ledger import allocate_v2_points_to_participant, create_collateral_market, split_complete_sets, merge_complete_sets
    from app.storage import DEMO_USER_ID, store_markets
    with IsolatedMigrationFixture() as f:
        c=f.conn; store_markets(c,sample_markets); c.commit(); migrate_fixture(f)
        c.execute('BEGIN'); install_checkpoint(c); c.commit()
        bootstrap_v2_point_supply(c,amount_micro=100000,idempotency_key='market-bootstrap')
        account=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=50000,idempotency_key='market-allocation')['destination_account_id']
        market=sample_markets[0]['market_id']
        assert not create_collateral_market(c,market_id=market)['idempotent_replay']
        assert c.execute("SELECT opening_value FROM balance_history_bucket WHERE kind='MARKET_RESERVE' AND market_id=?",(market,)).fetchone()[0]==0
        split=split_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='history-split')
        def events(source):
            return [tuple(r) for r in c.execute('''SELECT e.role,e.balance_before,e.delta,e.balance_after FROM balance_history_event e JOIN balance_history_operation o USING(operation_id) WHERE o.source_type='reserve_event' AND o.source_id=? ORDER BY e.event_seq''',(source,))]
        assert events(split['event_id'])==[('SPLIT_POINT',50000,-20000,30000),('SPLIT_RESERVE',0,20000,20000),('SPLIT_YES',0,2,2),('SPLIT_NO',0,2,2)]
        merge=merge_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='history-merge')
        assert events(merge['event_id'])==[('MERGE_POINT',30000,10000,40000),('MERGE_RESERVE',20000,-10000,10000),('MERGE_YES',2,-1,1),('MERGE_NO',2,-1,1)]
        with sqlite3.connect(f.path) as other:
            assert other.execute('SELECT reserve_micro FROM market_reserves WHERE market_id=?',(market,)).fetchone()[0]==10000
            assert other.execute('SELECT available_micro FROM point_accounts WHERE account_id=?',(account,)).fetchone()[0]==40000
            assert other.execute('SELECT count(*) FROM outcome_positions WHERE account_id=? AND market_id=? AND available_shares=1',(account,market)).fetchone()[0]==2
            assert other.execute("SELECT count(*) FROM balance_history_bucket WHERE market_id=? AND kind IN ('MARKET_RESERVE','SHARE_AVAILABLE','SHARE_LOCKED')",(market,)).fetchone()[0]==5
            assert other.execute('SELECT count(*) FROM reserve_events WHERE market_id=?',(market,)).fetchone()[0]==2
            assert other.execute("SELECT count(*) FROM collateral_ledger_entries WHERE reference_type='reserve_event'").fetchone()[0]==4
            assert other.execute("SELECT count(*) FROM demo_audit_events WHERE reference_type='reserve_event'").fetchone()[0]==2
            assert other.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'SPLIT_%' OR role LIKE 'MERGE_%'").fetchone()[0]==8
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert split_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='history-split')['idempotent_replay']
        assert create_collateral_market(c,market_id=market)['idempotent_replay']


def _history_market_setup(f, sample_markets):
    from app.collateral_ledger import allocate_v2_points_to_participant, create_collateral_market
    from app.storage import DEMO_USER_ID, store_markets
    c=f.conn; store_markets(c,sample_markets); c.commit(); migrate_fixture(f)
    c.execute('BEGIN'); install_checkpoint(c); c.commit()
    bootstrap_v2_point_supply(c,amount_micro=100000,idempotency_key='setup-bootstrap')
    account=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=50000,idempotency_key='setup-allocation')['destination_account_id']
    market=sample_markets[0]['market_id']; create_collateral_market(c,market_id=market)
    return account,market


def test_history_existing_position_replay_and_direct_guard(sample_markets):
    import sqlite3
    from app.collateral_ledger import CollateralLedgerError, split_complete_sets, merge_complete_sets, verify_collateral_invariants
    with IsolatedMigrationFixture() as f:
        account,market=_history_market_setup(f,sample_markets); c=f.conn
        split_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='first')
        split_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='second')
        assert c.execute("SELECT count(*) FROM balance_history_bucket WHERE account_id=? AND market_id=? AND kind IN ('SHARE_AVAILABLE','SHARE_LOCKED')",(account,market)).fetchone()[0]==4
        merge=merge_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='merge-all')
        assert merge_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='merge-all')['idempotent_replay']
        with pytest.raises(CollateralLedgerError,match='idempotency_conflict'):
            merge_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='merge-all')
        assert c.execute('SELECT available_micro FROM point_accounts WHERE account_id=?',(account,)).fetchone()[0]==50000
        assert c.execute('SELECT reserve_micro FROM market_reserves WHERE market_id=?',(market,)).fetchone()[0]==0
        assert verify_collateral_invariants(c)['integrity_status']=='verified'
        with sqlite3.connect(f.path) as other:
            other.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
                other.execute('UPDATE outcome_positions SET available_shares=available_shares+1 WHERE account_id=?',(account,))
        with pytest.raises(CollateralLedgerError,match='integer_overflow'):
            split_complete_sets(c,account_id=account,market_id=market,quantity=9223372036854775807,idempotency_key='overflow')
        with pytest.raises(CollateralLedgerError,match='insufficient_points'):
            split_complete_sets(c,account_id=account,market_id=market,quantity=6,idempotency_key='insufficient')


def test_history_split_partial_ledger_rolls_back_savepoint(sample_markets,monkeypatch):
    import sqlite3
    import app.collateral_ledger as ledger
    with IsolatedMigrationFixture() as f:
        account,market=_history_market_setup(f,sample_markets); c=f.conn
        before=c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]
        original=ledger._record_ledger
        def omit(conn,**kwargs):
            if kwargs['entry_type']!='split_reserve_credit': original(conn,**kwargs)
        monkeypatch.setattr(ledger,'_record_ledger',omit)
        c.execute('BEGIN'); c.execute('CREATE TABLE caller_marker(v INTEGER)')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            ledger.split_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='broken')
        assert c.in_transaction
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='caller_marker'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM reserve_events').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==before
        assert c.execute('SELECT count(*) FROM outcome_positions').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        assert c.execute('SELECT available_micro FROM point_accounts WHERE account_id=?',(account,)).fetchone()[0]==50000
        assert c.execute('SELECT reserve_micro FROM market_reserves WHERE market_id=?',(market,)).fetchone()[0]==0
        c.rollback()


def test_history_locked_shares_and_market_status_rejected(sample_markets):
    from test_v2_clob_migration import populated
    from app.collateral_ledger import CollateralLedgerError, merge_complete_sets, split_complete_sets
    with IsolatedMigrationFixture() as f:
        market,_,sell=populated(f,sample_markets); c=f.conn
        account=c.execute('SELECT account_id FROM order_collateral_reservations WHERE id=?',(sell['reservation_id'],)).fetchone()[0]
        migrate_fixture(f); c.execute('BEGIN'); install_checkpoint(c); c.commit()
        with pytest.raises(CollateralLedgerError,match='locked_shares_present'):
            merge_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='locked')
        with pytest.raises(CollateralLedgerError,match='market_missing'):
            split_complete_sets(c,account_id=account,market_id='not-a-market',quantity=1,idempotency_key='bad-market')
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0


def test_history_reserve_context_rejects_wrong_keys_roles_and_incomplete(sample_markets):
    import sqlite3
    from app.collateral_ledger import create_collateral_market
    from app.v2_balance_history import begin_reserve
    with IsolatedMigrationFixture() as f:
        account,market=_history_market_setup(f,sample_markets); c=f.conn
        other=sample_markets[1]['market_id']; create_collateral_market(c,market_id=other)
        treasury=c.execute("SELECT account_id FROM point_accounts WHERE owner_type='operator'").fetchone()[0]
        c.execute('BEGIN')
        op=begin_reserve(c,account_id=account,market_id=market,event_type='split',quantity=1,amount=10000)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute('UPDATE point_accounts SET available_micro=available_micro-10000 WHERE account_id=?',(account,))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("""INSERT INTO reserve_events(engine_key,market_id,account_id,event_type,quantity,points_micro,reserve_before_micro,reserve_after_micro,idempotency_key,payload_hash,created_at) VALUES ('collateralized_clob_v2',?,?,'split',1,10000,0,10000,'bad','hash','t')""",(other,account))
        c.execute("""INSERT INTO reserve_events(engine_key,market_id,account_id,event_type,quantity,points_micro,reserve_before_micro,reserve_after_micro,idempotency_key,payload_hash,created_at) VALUES ('collateralized_clob_v2',?,?,'split',1,10000,0,10000,'pending','hash','t')""",(market,account))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute('UPDATE point_accounts SET available_micro=available_micro-10000 WHERE account_id=?',(treasury,))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute('UPDATE market_reserves SET reserve_micro=10000,net_complete_sets=1 WHERE market_id=?',(other,))
        b=c.execute("SELECT generation_id,bucket_id FROM balance_history_bucket WHERE kind='POINT_AVAILABLE' AND account_id=?",(account,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_event_invalid'):
            c.execute("INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after) VALUES (?,?,?,'SPLIT_YES',50000,1,50001)",(b[0],b[1],op))
        c.execute('UPDATE point_accounts SET available_micro=40000 WHERE account_id=?',(account,))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_event_invalid'):
            c.execute("INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after) VALUES (?,?,?,'SPLIT_POINT',50000,-10000,40000)",(b[0],b[1],op))
        c.execute('UPDATE market_reserves SET reserve_micro=10000,net_complete_sets=1 WHERE market_id=?',(market,))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("INSERT INTO outcome_positions(account_id,market_id,outcome,available_shares,updated_at) VALUES (?,?,'NO',0,'t')",(account,market))
        source=c.execute('SELECT id FROM reserve_events').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            c.execute("INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,market_id,amount,payload_hash) VALUES (?,'reserve_event',?,?,?,10000,'hash')",(op,source,account,market))
        with pytest.raises(sqlite3.IntegrityError): c.commit()
        c.rollback()
        assert c.execute('SELECT count(*) FROM reserve_events').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0


def test_history_split_outer_rollback_and_frozen_market(sample_markets):
    from app.collateral_ledger import CollateralLedgerError, split_complete_sets
    with IsolatedMigrationFixture() as f:
        account,market=_history_market_setup(f,sample_markets); c=f.conn
        before=c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]
        c.execute('BEGIN')
        split_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='outer')
        assert c.in_transaction
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'SPLIT_%'").fetchone()[0]==4
        c.rollback()
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==before
        assert c.execute('SELECT count(*) FROM reserve_events').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM outcome_positions').fetchone()[0]==0
        c.execute("UPDATE collateral_markets SET status='frozen' WHERE market_id=?",(market,)); c.commit()
        with pytest.raises(CollateralLedgerError,match='market_not_open'):
            split_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='frozen')


def test_history_order_reserve_cancel_reject_buy_sell(sample_markets):
    import sqlite3
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import split_complete_sets,reserve_v2_order_collateral,cancel_v2_order_collateral,reject_v2_order_collateral,verify_collateral_invariants
    with IsolatedMigrationFixture() as f:
        account,market=_history_market_setup(f,sample_markets); c=f.conn
        split_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='order-split')
        for side in ('BUY','SELL'):
            for reason in ('cancel','reject'):
                order=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side=side,outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key=side+'-'+reason+'-reserve')
                if reason=='cancel':
                    released=cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key=side+'-cancel')
                else:
                    released=reject_v2_order_collateral(c,reservation_id=order['reservation_id'],idempotency_key=side+'-reject')
                assert released['status']=='released'
                assert verify_collateral_invariants(c)['integrity_status']=='verified'
        rows=c.execute("""SELECT e.role,e.delta,o.source_type FROM balance_history_event e JOIN balance_history_operation o USING(operation_id)
           WHERE o.source_type='order_collateral_event' ORDER BY e.event_seq""").fetchall()
        assert len(rows)==16
        assert [r[0] for r in rows]==['ORDER_AVAILABLE','ORDER_LOCKED']*8
        assert [r[1] for r in rows]==[-1000,1000,1000,-1000]*2+[-1,1,1,-1]*2
        assert c.execute("SELECT count(*) FROM order_collateral_events").fetchone()[0]==8
        assert c.execute("SELECT count(*) FROM order_collateral_ledger_entries").fetchone()[0]==16
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        with sqlite3.connect(f.path) as other:
            assert other.execute("SELECT count(*) FROM balance_history_operation WHERE source_type='order_collateral_event'").fetchone()[0]==8
            assert other.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ORDER_%'").fetchone()[0]==16


def _history_order_setup(f,sample_markets):
    from app.collateral_ledger import split_complete_sets
    account,market=_history_market_setup(f,sample_markets)
    split_complete_sets(f.conn,account_id=account,market_id=market,quantity=2,idempotency_key='prepare-orders')
    return account,market


def test_order_history_replay_owner_conflict_and_direct_write(sample_markets):
    import sqlite3
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import CollateralLedgerError,reserve_v2_order_collateral,cancel_v2_order_collateral
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        args=dict(participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='once')
        order=reserve_v2_order_collateral(c,**args);assert reserve_v2_order_collateral(c,**args)['idempotent_replay']
        with pytest.raises(CollateralLedgerError,match='idempotency_conflict'):
            reserve_v2_order_collateral(c,**{**args,'quantity':2})
        with pytest.raises(CollateralLedgerError,match='reservation_not_owned'):
            cancel_v2_order_collateral(c,participant_id='other-user',reservation_id=order['reservation_id'],idempotency_key='wrong-owner')
        cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key='cancel')
        assert cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key='cancel')['idempotent_replay']
        with pytest.raises(CollateralLedgerError,match='reservation_not_reserved'):
            cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key='cancel-again')
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role LIKE 'ORDER_%'").fetchone()[0]==4
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        with sqlite3.connect(f.path) as other:
            other.execute('PRAGMA foreign_keys=ON')
            with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
                other.execute('UPDATE point_accounts SET locked_micro=locked_micro+1 WHERE account_id=?',(account,))


def test_order_history_ledger_failure_and_savepoint(sample_markets,monkeypatch):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        before=c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]
        monkeypatch.setattr(ledger,'_write_order_event',lambda *a,**kw: None)
        c.execute('BEGIN');c.execute('CREATE TABLE caller_marker(value INTEGER)')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            ledger.reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='bad-ledger')
        assert c.in_transaction
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name='caller_marker'").fetchone()[0]==1
        assert c.execute('SELECT count(*) FROM order_collateral_reservations').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM order_collateral_events').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==before
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        c.rollback()


def test_order_history_unfinalized_and_wrong_target_rejected(sample_markets):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    from app.v2_balance_history import begin_order
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        order=ledger.reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='for-context')
        reservation=c.execute('SELECT * FROM order_collateral_reservations WHERE id=?',(order['reservation_id'],)).fetchone()
        row=c.execute('SELECT available_micro,locked_micro FROM point_accounts WHERE account_id=?',(account,)).fetchone();available,locked=row
        treasury=c.execute("SELECT account_id FROM point_accounts WHERE owner_type='operator'").fetchone()[0]
        c.execute('BEGIN')
        op=begin_order(c,reservation=reservation,event_type='release')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute('UPDATE point_accounts SET available_micro=available_micro+1000,locked_micro=locked_micro-1000 WHERE account_id=?',(account,))
        wrong=dict(reservation);wrong['account_id']=treasury
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            ledger._insert_order_event(c,wrong,'release','cancelled',available,available+1000,locked,locked-1000,'wrong-source','hash',None,'t')
        source=ledger._insert_order_event(c,reservation,'release','cancelled',available,available+1000,locked,locked-1000,'unfinished','hash',None,'t')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute('UPDATE point_accounts SET available_micro=available_micro+1000,locked_micro=locked_micro-1000 WHERE account_id=?',(treasury,))
        bucket=c.execute("SELECT generation_id,bucket_id FROM balance_history_bucket WHERE account_id=? AND kind='POINT_AVAILABLE'",(account,)).fetchone()
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_event_invalid'):
            c.execute("INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after) VALUES (?,?,?,'ORDER_LOCKED',?,1000,?)",(bucket[0],bucket[1],op,available,available+1000))
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            c.execute("INSERT INTO balance_history_operation(operation_id,source_type,source_id,account_id,market_id,amount,payload_hash) VALUES (?,'order_collateral_event',?,?,?,1000,'hash')",(op,source,account,market))
        with pytest.raises(sqlite3.IntegrityError):c.commit()
        c.rollback()
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0


@pytest.mark.parametrize('failure_step',['source','ledger','audit'])
def test_order_history_injected_failure_rolls_back_everything(sample_markets,monkeypatch,failure_step):
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        tables=('point_accounts','outcome_positions','order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries','demo_audit_events','balance_history_event','balance_history_operation','balance_history_context','sqlite_sequence')
        before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        name={'source':'_insert_order_event','ledger':'_write_order_event','audit':'insert_audit_event'}[failure_step]
        original=getattr(ledger,name)
        def fail(*a,**kw):
            original(*a,**kw)
            raise RuntimeError('injected-'+failure_step)
        monkeypatch.setattr(ledger,name,fail)
        with pytest.raises(RuntimeError,match='injected-'+failure_step):
            ledger.reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='SELL',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='failure')
        assert not c.in_transaction
        assert before=={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}


def test_order_history_release_failure_and_contract_two(sample_markets,monkeypatch):
    import app.v2_balance_history as history
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import CollateralLedgerError,reserve_v2_order_collateral,cancel_v2_order_collateral
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        order=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='release-fail')
        tables=('point_accounts','order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries','demo_audit_events','balance_history_event','balance_history_operation','balance_history_context','sqlite_sequence')
        before={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        with monkeypatch.context() as patch:
            patch.setattr(history,'finalize_order',lambda *a,**kw: (_ for _ in ()).throw(RuntimeError('finalize-injected')))
            with pytest.raises(RuntimeError,match='finalize-injected'):
                cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key='cancel-fail')
        assert before=={t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        cur=c.execute("""INSERT INTO order_collateral_reservations(engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,status,created_at,updated_at,contract_version,remaining_collateral,consumed_collateral,released_collateral)
            SELECT engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,status,created_at,updated_at,2,remaining_collateral,consumed_collateral,released_collateral FROM order_collateral_reservations WHERE id=?""",(order['reservation_id'],));c.commit()
        count=c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]
        with pytest.raises(CollateralLedgerError,match='unsupported_reservation_contract'):
            cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=cur.lastrowid,idempotency_key='cancel-v2')
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==count
        assert c.execute('SELECT status FROM order_collateral_reservations WHERE id=?',(cur.lastrowid,)).fetchone()[0]=='reserved'


def test_order_history_insufficient_and_overflow(sample_markets):
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import CollateralLedgerError,reserve_v2_order_collateral
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        base=dict(participant_id=DEMO_USER_ID,market_id=market,outcome='YES',limit_price_micro=1000,idempotency_key='invalid')
        with pytest.raises(CollateralLedgerError,match='insufficient_points'):
            reserve_v2_order_collateral(c,**base,side='BUY',quantity=100)
        with pytest.raises(CollateralLedgerError,match='insufficient_shares'):
            reserve_v2_order_collateral(c,**base,side='SELL',quantity=3)
        with pytest.raises(CollateralLedgerError,match='integer_overflow'):
            reserve_v2_order_collateral(c,**base,side='BUY',quantity=9223372036854775807)
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM order_collateral_reservations').fetchone()[0]==0


def test_order_history_wrong_outcome_and_duplicate_are_rejected(sample_markets):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    from app.v2_balance_history import begin_order
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        order=ledger.reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='SELL',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='sell-wrong')
        r=c.execute('SELECT * FROM order_collateral_reservations WHERE id=?',(order['reservation_id'],)).fetchone()
        pos=c.execute("SELECT available_shares,locked_shares FROM outcome_positions WHERE account_id=? AND market_id=? AND outcome='YES'",(account,market)).fetchone()
        c.execute('BEGIN')
        op=begin_order(c,reservation=r,event_type='release')
        source=ledger._insert_order_event(c,r,'release','cancelled',pos[0],pos[0]+1,pos[1],pos[1]-1,'pending-sell','hash',None,'t')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            c.execute("UPDATE outcome_positions SET available_shares=available_shares+1,locked_shares=locked_shares-1 WHERE account_id=? AND market_id=? AND outcome='NO'",(account,market))
        b=c.execute("SELECT generation_id,bucket_id FROM balance_history_bucket WHERE account_id=? AND market_id=? AND outcome='YES' AND kind='SHARE_AVAILABLE'",(account,market)).fetchone()
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_event_invalid'):
            c.execute("INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after) VALUES (?,?,?,'ORDER_AVAILABLE',?,1,?)",(b[0],b[1],op,pos[0],pos[0]+1))
        c.execute("UPDATE outcome_positions SET available_shares=available_shares+1,locked_shares=locked_shares-1 WHERE account_id=? AND market_id=? AND outcome='YES'",(account,market))
        with pytest.raises(sqlite3.IntegrityError):
            c.execute("INSERT INTO balance_history_event(generation_id,bucket_id,operation_id,role,balance_before,delta,balance_after) VALUES (?,?,?,'ORDER_AVAILABLE',?,1,?)",(b[0],b[1],op,pos[0],pos[0]+1))
        with pytest.raises(sqlite3.IntegrityError):c.commit()
        c.rollback()
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0


def test_order_history_valid_hash_wrong_audit_payload_rolls_back(sample_markets,monkeypatch):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        before=c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]
        original=ledger.insert_audit_event
        def wrong(conn,**kwargs):
            if kwargs.get('event_type')=='v2_order_collateral_reserved':
                kwargs['after']={**kwargs['after'],'collateral_amount':kwargs['after']['collateral_amount']+1}
            return original(conn,**kwargs)
        monkeypatch.setattr(ledger,'insert_audit_event',wrong)
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            ledger.reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='wrong-audit')
        assert c.execute('SELECT count(*) FROM order_collateral_reservations').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==before


def test_order_history_missing_locked_event_cannot_finalize(sample_markets):
    import sqlite3
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import reserve_v2_order_collateral
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        c.execute("""CREATE TEMP TRIGGER inject_missing_order_event BEFORE INSERT ON main.balance_history_event
            WHEN NEW.role='ORDER_LOCKED' BEGIN SELECT RAISE(IGNORE); END""")
        before=c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_manifest_invalid'):
            reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='missing-event')
        assert c.execute('SELECT count(*) FROM order_collateral_reservations').fetchone()[0]==0
        assert c.execute('SELECT count(*) FROM balance_history_event').fetchone()[0]==before
        assert c.execute('SELECT count(*) FROM balance_history_context').fetchone()[0]==0


def test_order_history_successful_reserve_and_release_outer_rollback(sample_markets):
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import reserve_v2_order_collateral,cancel_v2_order_collateral
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        tables=('point_accounts','order_collateral_reservations','order_collateral_events','order_collateral_ledger_entries','demo_audit_events','balance_history_event','balance_history_operation','balance_history_context','sqlite_sequence')
        snapshot=lambda: {t:[tuple(r) for r in c.execute('SELECT * FROM '+t)] for t in tables}
        before=snapshot(); c.execute('BEGIN')
        reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='outer-reserve')
        assert c.in_transaction; c.rollback(); assert snapshot()==before
        order=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='committed-reserve')
        before=snapshot();c.execute('BEGIN')
        cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key='outer-cancel')
        assert c.in_transaction;c.rollback();assert snapshot()==before


def test_order_history_second_source_for_same_reservation_rejected(sample_markets):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    from app.v2_balance_history import begin_order
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        order=ledger.reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='single-source')
        r=c.execute('SELECT * FROM order_collateral_reservations WHERE id=?',(order['reservation_id'],)).fetchone()
        available,locked=c.execute('SELECT available_micro,locked_micro FROM point_accounts WHERE account_id=?',(account,)).fetchone()
        c.execute('BEGIN');begin_order(c,reservation=r,event_type='reserve')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            ledger._insert_order_event(c,r,'reserve',None,available,available-1000,locked,locked+1000,'duplicate-source','hash',None,'t')
        c.rollback()
        assert c.execute('SELECT count(*) FROM order_collateral_events WHERE reservation_id=?',(r['id'],)).fetchone()[0]==1


def test_order_history_frozen_market_rejects_direct_reserve_source(sample_markets):
    import sqlite3
    import app.collateral_ledger as ledger
    from app.storage import DEMO_USER_ID
    from app.v2_balance_history import begin_order
    with IsolatedMigrationFixture() as f:
        account,market=_history_order_setup(f,sample_markets);c=f.conn
        c.execute('BEGIN');c.execute("UPDATE collateral_markets SET status='frozen' WHERE market_id=?",(market,))
        cur=c.execute("""INSERT INTO order_collateral_reservations(engine_key,account_id,participant_id,market_id,side,outcome,quantity,limit_price_micro,collateral_type,collateral_amount,status,created_at,updated_at,contract_version,remaining_collateral,consumed_collateral,released_collateral) VALUES ('collateralized_clob_v2',?,?,?,'BUY','YES',1,1000,'point',1000,'reserved','t','t',1,1000,0,0)""",(account,DEMO_USER_ID,market))
        r=c.execute('SELECT * FROM order_collateral_reservations WHERE id=?',(cur.lastrowid,)).fetchone()
        available,locked=c.execute('SELECT available_micro,locked_micro FROM point_accounts WHERE account_id=?',(account,)).fetchone()
        begin_order(c,reservation=r,event_type='reserve')
        with pytest.raises(sqlite3.IntegrityError,match='balance_history_source_protocol_unavailable'):
            ledger._insert_order_event(c,r,'reserve',None,available,available-1000,locked,locked+1000,'frozen','hash',None,'t')
        c.rollback()


def test_order_history_releases_legacy_reservations_after_checkpoint(sample_markets):
    from test_v2_clob_migration import populated
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import cancel_v2_order_collateral,verify_collateral_invariants
    with IsolatedMigrationFixture() as f:
        _,buy,sell=populated(f,sample_markets);c=f.conn
        migrate_fixture(f);c.execute('BEGIN');install_checkpoint(c);c.commit()
        watermark=c.execute('SELECT order_watermark FROM balance_history_generation').fetchone()[0]
        for order,key in ((buy,'old-buy-cancel'),(sell,'old-sell-cancel')):
            assert cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key=key)['status']=='released'
        sources=[r[0] for r in c.execute("SELECT source_id FROM balance_history_operation WHERE source_type='order_collateral_event' ORDER BY source_id")]
        assert len(sources)==2 and all(source>watermark for source in sources)
        assert c.execute("SELECT count(*) FROM balance_history_event WHERE role IN ('ORDER_AVAILABLE','ORDER_LOCKED')").fetchone()[0]==4
        assert verify_collateral_invariants(c)['integrity_status']=='verified'


def test_history_migration_checkpoint_and_reconciliation(sample_markets):
    import sqlite3
    from test_v2_clob_migration import populated
    from app.v2_balance_history import verify_history
    with IsolatedMigrationFixture() as f:
        populated(f,sample_markets)
        c=f.conn
        before=[tuple(r) for r in c.execute('SELECT * FROM demo_audit_events ORDER BY id')]
        assert migrate_fixture(f,with_history=True)
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_key_check').fetchall()==[]
        assert [tuple(r) for r in c.execute('SELECT * FROM demo_audit_events ORDER BY id')]==before
        assert {r[0] for r in c.execute('SELECT DISTINCT kind FROM balance_history_bucket')}=={'POINT_AVAILABLE','POINT_LOCKED','SHARE_AVAILABLE','SHARE_LOCKED','MARKET_RESERVE'}
        assert verify_history(c)['integrity_status']=='verified'
        assert not migrate_fixture(f,with_history=True)
        with sqlite3.connect(f.path) as other:
            other.execute('PRAGMA foreign_keys=ON')
            assert verify_history(other)['integrity_status']=='verified'


def _history_migration_full(f,sample_markets):
    from app.storage import DEMO_USER_ID,store_markets
    from app.collateral_ledger import allocate_v2_points_to_participant,create_collateral_market,split_complete_sets,merge_complete_sets,reserve_v2_order_collateral,cancel_v2_order_collateral,reject_v2_order_collateral
    c=f.conn;store_markets(c,sample_markets);c.commit();migrate_fixture(f,with_history=True)
    bootstrap_v2_point_supply(c,amount_micro=100000,idempotency_key='reconcile-bootstrap')
    a=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=50000,idempotency_key='reconcile-allocation')['destination_account_id']
    m=sample_markets[0]['market_id'];create_collateral_market(c,market_id=m)
    split_complete_sets(c,account_id=a,market_id=m,quantity=2,idempotency_key='reconcile-split')
    merge_complete_sets(c,account_id=a,market_id=m,quantity=1,idempotency_key='reconcile-merge')
    for side in ('BUY','SELL'):
        for reason in ('cancel','reject'):
            order=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=m,side=side,outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key=side+reason+'-reserve')
            if reason=='cancel':cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key=side+reason)
            else:reject_v2_order_collateral(c,reservation_id=order['reservation_id'],idempotency_key=side+reason)
    return a,m


def test_history_reconciliation_all_writers_is_readonly(sample_markets):
    from app.v2_balance_history import verify_history
    from app.v2_clob_invariants import verify_v2
    with IsolatedMigrationFixture() as f:
        _history_migration_full(f,sample_markets);c=f.conn
        before=list(c.iterdump());changes=c.total_changes
        assert verify_history(c)['integrity_status']=='verified'
        assert verify_v2(c)['integrity_status']=='verified'
        assert c.total_changes==changes and list(c.iterdump())==before


@pytest.mark.parametrize('kind',['checkpoint','checkpoint_kind_count','audit_boundary','bucket_missing','bucket_extra','event_shift','event_missing','event_order','role','duplicate_role','source_payload','source_hash_pair','source_missing','extra_source','extra_ledger','ledger_request','audit_payload','trigger_missing','index_missing','partial_table','live_balance','bucket_creation'])
def test_history_tamper_is_detected_on_independent_copy(sample_markets,tmp_path,kind):
    import sqlite3
    from app.v2_balance_history import verify_history
    from app.v2_clob_invariants import verify_v2
    with IsolatedMigrationFixture() as f:
        _history_migration_full(f,sample_markets)
        path=tmp_path/(kind+'.sqlite')
        with sqlite3.connect(path) as out: f.conn.backup(out)
    c=sqlite3.connect(path);c.row_factory=sqlite3.Row;c.execute('PRAGMA foreign_keys=OFF')
    def mutate(trigger,sql):
        ddl=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name=?",(trigger,)).fetchone()[0]
        c.execute('DROP TRIGGER "'+trigger+'"');c.execute(sql);c.execute(ddl)
    if kind=='checkpoint': mutate('balance_history_generation_immutable_update',"UPDATE balance_history_generation SET checkpoint_sha256='"+'0'*64+"'")
    elif kind=='checkpoint_kind_count': mutate('balance_history_generation_immutable_update',"UPDATE balance_history_generation SET point_available_count=point_available_count+1")
    elif kind=='audit_boundary': mutate('balance_history_generation_immutable_update',"UPDATE balance_history_generation SET audit_boundary_id=audit_boundary_id+1000")
    elif kind=='bucket_missing': mutate('balance_history_bucket_immutable_delete',"DELETE FROM balance_history_bucket WHERE bucket_id=(SELECT min(bucket_id) FROM balance_history_bucket)")
    elif kind=='bucket_extra': mutate('balance_history_bucket_insert_guard',"INSERT INTO balance_history_bucket(generation_id,kind,account_id,opening_value,origin,creation_operation_id) SELECT generation_id,'POINT_AVAILABLE','ghost',0,'created',(SELECT operation_id FROM balance_history_operation WHERE source_type='point_allocation_event' LIMIT 1) FROM balance_history_generation")
    elif kind=='event_shift': mutate('balance_history_event_immutable_update',"UPDATE balance_history_event SET balance_before=balance_before+1,balance_after=balance_after+1 WHERE event_seq=(SELECT max(event_seq) FROM balance_history_event)")
    elif kind=='event_missing': mutate('balance_history_event_immutable_delete',"DELETE FROM balance_history_event WHERE event_seq=(SELECT max(event_seq) FROM balance_history_event)")
    elif kind=='event_order': mutate('balance_history_event_immutable_update',"UPDATE balance_history_event SET event_seq=(SELECT max(event_seq)+1 FROM balance_history_event) WHERE role='ALLOCATION_DEBIT'")
    elif kind=='role': mutate('balance_history_event_immutable_update',"UPDATE balance_history_event SET role='ALLOCATION_DEBIT' WHERE event_seq=(SELECT max(event_seq) FROM balance_history_event)")
    elif kind=='duplicate_role': mutate('balance_history_event_immutable_update',"UPDATE balance_history_event SET role='ORDER_AVAILABLE' WHERE role='ORDER_LOCKED' AND event_seq=(SELECT max(event_seq) FROM balance_history_event)")
    elif kind=='extra_source':
        ddl=c.execute("SELECT sql FROM sqlite_master WHERE name='balance_history_allocation_source'").fetchone()[0]
        c.execute('DROP TRIGGER balance_history_allocation_source')
        c.execute("""INSERT INTO point_allocation_events(engine_key,source_account_id,destination_account_id,participant_id,amount_micro,source_before_micro,source_after_micro,destination_before_micro,destination_after_micro,idempotency_key,request_id,payload_hash,created_at) SELECT engine_key,source_account_id,destination_account_id,participant_id,amount_micro,source_before_micro,source_after_micro,destination_before_micro,destination_after_micro,'extra-source',request_id,payload_hash,created_at FROM point_allocation_events LIMIT 1""")
        c.execute(ddl)
    elif kind=='audit_payload':
        from app.storage import _audit_integrity_payload,_audit_event_hash,_canonical_json
        import json
        ddl=[(r['name'],r['sql']) for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='demo_audit_events' AND name LIKE 'balance_history_%'")]
        for name,_ in ddl:c.execute('DROP TRIGGER "'+name+'"')
        rows=[dict(r) for r in c.execute('SELECT * FROM demo_audit_events ORDER BY id')];previous=''
        for row in rows:
            if row['event_type']=='v2_participant_points_allocated':
                payload=json.loads(row['after_json']);payload['amount_micro']+=1;row['after_json']=_canonical_json(payload)
            payload=_canonical_json(_audit_integrity_payload(event_type=row['event_type'],user_id=row['user_id'],route=row['route'],request_id=row['request_id'],reference_type=row['reference_type'],reference_id=str(row['reference_id']) if row['reference_id'] is not None else None,before_json=row['before_json'],after_json=row['after_json'],note=row['note'],created_at=row['created_at']))
            event_hash=_audit_event_hash(previous,payload)
            c.execute('UPDATE demo_audit_events SET after_json=?,previous_event_hash=?,integrity_payload_json=?,event_hash=? WHERE id=?',(row['after_json'],previous,payload,event_hash,row['id']))
            previous=event_hash
        for _,sql in ddl:c.execute(sql)
    elif kind=='source_payload': mutate('balance_history_operation_immutable_update',"UPDATE balance_history_operation SET payload_hash='tampered' WHERE source_type='point_allocation_event'")
    elif kind=='source_hash_pair':
        names=('balance_history_operation_immutable_update','balance_history_allocation_source_final_update')
        ddl=[c.execute('SELECT sql FROM sqlite_master WHERE name=?',(name,)).fetchone()[0] for name in names]
        for name in names:c.execute('DROP TRIGGER '+name)
        c.execute("UPDATE balance_history_operation SET payload_hash='same-false-hash' WHERE source_type='point_allocation_event'")
        c.execute("UPDATE point_allocation_events SET payload_hash='same-false-hash'")
        for sql in ddl:c.execute(sql)
    elif kind=='ledger_request': mutate('balance_history_allocation_collateral_ledger_entries_update',"UPDATE collateral_ledger_entries SET request_id='different' WHERE reference_type='point_allocation_event'")
    elif kind=='source_missing': mutate('balance_history_allocation_source_final_delete',"DELETE FROM point_allocation_events WHERE id=(SELECT min(id) FROM point_allocation_events)")
    elif kind=='extra_ledger':
        ddl=c.execute("SELECT sql FROM sqlite_master WHERE name='balance_history_allocation_collateral_ledger_entries_insert'").fetchone()[0]
        c.execute('DROP TRIGGER balance_history_allocation_collateral_ledger_entries_insert')
        c.execute("""INSERT INTO collateral_ledger_entries(engine_key,account_id,market_id,entry_type,amount_micro,account_available_before_micro,account_available_after_micro,reserve_before_micro,reserve_after_micro,reference_type,reference_id,request_id,created_at) SELECT engine_key,account_id,market_id,entry_type,amount_micro,account_available_before_micro,account_available_after_micro,reserve_before_micro,reserve_after_micro,reference_type,reference_id,request_id,created_at FROM collateral_ledger_entries WHERE reference_type='point_allocation_event' LIMIT 1""")
        c.execute(ddl)
    elif kind=='trigger_missing': c.execute('DROP TRIGGER balance_history_context_delete_guard')
    elif kind=='index_missing': c.execute('DROP INDEX balance_history_event_bucket')
    elif kind=='partial_table': c.execute('DROP TABLE balance_history_event')
    elif kind=='live_balance':
        ddl=[(r['name'],r['sql']) for r in c.execute("SELECT name,sql FROM sqlite_master WHERE type='trigger' AND tbl_name='point_accounts' AND name LIKE 'balance_history_%'")]
        for name,_ in ddl:c.execute('DROP TRIGGER "'+name+'"')
        c.execute("UPDATE point_accounts SET available_micro=available_micro+1 WHERE owner_type='participant'")
        for _,sql in ddl:c.execute(sql)
    elif kind=='bucket_creation': mutate('balance_history_bucket_immutable_update',"UPDATE balance_history_bucket SET creation_operation_id=(SELECT operation_id FROM balance_history_operation WHERE source_type='reserve_event' LIMIT 1) WHERE kind='POINT_LOCKED' AND origin='created' AND account_id LIKE '%:participant:%'")
    c.commit();c.execute('PRAGMA foreign_keys=ON')
    if kind=='audit_payload':
        from app.storage import verify_audit_chain
        assert verify_audit_chain(c)['integrity_status']=='verified'
    before=list(c.iterdump());result=verify_history(c)
    assert result['integrity_status']=='failed',kind
    assert verify_v2(c)['integrity_status']=='failed'
    assert list(c.iterdump())==before
    c.close()


@pytest.mark.parametrize('stage',['shadows','copy_order_collateral_reservations','copy_order_collateral_events','copy_order_collateral_ledger_entries','drop_old','rename','indexes','precommit','history_partial','history_checkpoint','history_precommit'])
def test_history_migration_failure_rolls_back_and_retries(sample_markets,stage):
    from test_v2_clob_migration import populated,snapshot
    with IsolatedMigrationFixture() as f:
        populated(f,sample_markets);c=f.conn;before=snapshot(c)
        def fail(current):
            if current==stage:raise RuntimeError('injected')
        with pytest.raises(RuntimeError,match='injected'):migrate_fixture(f,with_history=True,failure_hook=fail)
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert snapshot(c)==before
        assert migrate_fixture(f,with_history=True)


def test_history_migration_unfinalized_context_rolls_back():
    with IsolatedMigrationFixture() as f:
        c=f.conn
        def inject(stage):
            if stage=='history_precommit':
                ddl=c.execute("SELECT sql FROM sqlite_master WHERE type='trigger' AND name='balance_history_context_fk_guard'").fetchone()[0]
                c.execute('DROP TRIGGER balance_history_context_fk_guard')
                c.execute("INSERT INTO balance_history_context(operation_id,guard) VALUES ('unfinalized',1)")
                c.execute(ddl)
        with pytest.raises(RuntimeError,match='history_invalid'):
            migrate_fixture(f,with_history=True,failure_hook=inject)
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'balance_history_%'").fetchone()[0]==0
        assert migrate_fixture(f,with_history=True)


def test_history_required_and_fk_disabled_are_rejected():
    from app.v2_balance_history import verify_history,HistoryError
    from app.v2_clob_invariants import verify_v2
    with IsolatedMigrationFixture() as f:
        c=f.conn;migrate_fixture(f)
        assert verify_history(c)['violation_codes']==['history_missing']
        assert verify_v2(c)['integrity_status']=='verified'
        c.execute('PRAGMA foreign_keys=OFF');c.execute('BEGIN')
        with pytest.raises(HistoryError,match='foreign_keys_required'):install_checkpoint(c)
        c.rollback();c.execute('PRAGMA foreign_keys=ON');c.execute('BEGIN');install_checkpoint(c);c.commit()
        c.execute('PRAGMA foreign_keys=OFF')
        assert 'history_foreign_keys_disabled' in verify_history(c)['violation_codes']


def test_history_migration_empty_and_fk_fault_rejects():
    from app.v2_balance_history import verify_history
    with IsolatedMigrationFixture() as f:
        c=f.conn
        def inject(stage):
            if stage=='history_precommit':
                ddl=c.execute("SELECT sql FROM sqlite_master WHERE name='balance_history_bucket_insert_guard'").fetchone()[0]
                c.execute('DROP TRIGGER balance_history_bucket_insert_guard')
                c.execute("INSERT INTO balance_history_bucket(generation_id,kind,account_id,opening_value,origin,creation_operation_id) VALUES ('nonexistent','POINT_AVAILABLE','ghost',0,'created','missing')")
                c.execute(ddl)
        with pytest.raises(RuntimeError,match='history_invalid'):
            migrate_fixture(f,with_history=True,failure_hook=inject)
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert c.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'balance_history_%'").fetchone()[0]==0
        assert migrate_fixture(f,with_history=True)
        assert c.execute('SELECT checkpoint_count FROM balance_history_generation').fetchone()[0]==0
        assert verify_history(c)['integrity_status']=='verified'


def test_history_migration_preserves_all_v1_rows_sequences_and_atomic_visibility(sample_markets):
    import sqlite3
    from test_v2_clob_migration import populated
    from app.v2_balance_history import verify_history
    with IsolatedMigrationFixture() as f:
        populated(f,sample_markets);c=f.conn
        names=[r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'")]
        columns={name:[r[1] for r in c.execute('PRAGMA table_info("'+name+'")')] for name in names}
        def rows(name):
            selected=','.join('"'+x.replace('"','""')+'"' for x in columns[name])
            return [tuple(r) for r in c.execute('SELECT '+selected+' FROM "'+name+'"')]
        before={name:rows(name) for name in names}
        sequences=dict(c.execute('SELECT name,seq FROM sqlite_sequence'))
        seen=[]
        def inspect(stage):
            if stage in ('history_partial','history_checkpoint','history_precommit'):
                assert c.in_transaction and c.execute('PRAGMA foreign_keys').fetchone()[0]==0
                with sqlite3.connect(f.path,timeout=1) as other:
                    assert other.execute('PRAGMA user_version').fetchone()[0]==1
                    assert other.execute("SELECT count(*) FROM sqlite_master WHERE name LIKE 'balance_history_%'").fetchone()[0]==0
                seen.append(stage)
        assert migrate_fixture(f,with_history=True,failure_hook=inspect)
        assert seen==['history_partial','history_checkpoint','history_precommit']
        assert before=={name:rows(name) for name in names}
        after=dict(c.execute('SELECT name,seq FROM sqlite_sequence'))
        assert all(after.get(name,-1)>=value for name,value in sequences.items())
        assert verify_history(c)['integrity_status']=='verified'


def test_history_migration_with_legacy_reservation_and_preflight_rejection(sample_markets,monkeypatch):
    import app.v2_clob_migration as migration
    from test_v2_clob_migration import populated
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import cancel_v2_order_collateral
    from app.v2_balance_history import verify_history
    with pytest.raises(TypeError,match='fixture_required'):migrate_fixture(object(),with_history=True)
    with IsolatedMigrationFixture() as f:
        _,buy,sell=populated(f,sample_markets);c=f.conn
        with monkeypatch.context() as patch:
            patch.setattr(migration,'_check_capacity',lambda *a: (_ for _ in ()).throw(RuntimeError('capacity_insufficient')))
            with pytest.raises(RuntimeError,match='capacity_insufficient'):migrate_fixture(f,with_history=True)
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert migrate_fixture(f,with_history=True)
        for order,key in ((buy,'history-old-buy'),(sell,'history-old-sell')):
            cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key=key)
        assert verify_history(c)['integrity_status']=='verified'


def test_history_reconciliation_preserves_request_ids(sample_markets):
    from app.storage import DEMO_USER_ID,store_markets
    from app.collateral_ledger import allocate_v2_points_to_participant,create_collateral_market,split_complete_sets,merge_complete_sets,reserve_v2_order_collateral,cancel_v2_order_collateral,verify_collateral_invariants
    from app.v2_balance_history import verify_history
    with IsolatedMigrationFixture() as f:
        c=f.conn;store_markets(c,sample_markets);c.commit();migrate_fixture(f,with_history=True)
        bootstrap_v2_point_supply(c,amount_micro=100000,idempotency_key='req-bootstrap',request_id='req-bootstrap')
        account=allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=50000,idempotency_key='req-allocation',request_id='req-allocation')['destination_account_id']
        market=sample_markets[0]['market_id'];create_collateral_market(c,market_id=market)
        split_complete_sets(c,account_id=account,market_id=market,quantity=2,idempotency_key='req-split',request_id='req-split')
        merge_complete_sets(c,account_id=account,market_id=market,quantity=1,idempotency_key='req-merge',request_id='req-merge')
        order=reserve_v2_order_collateral(c,participant_id=DEMO_USER_ID,market_id=market,side='BUY',outcome='YES',quantity=1,limit_price_micro=1000,idempotency_key='req-reserve',request_id='req-reserve')
        cancel_v2_order_collateral(c,participant_id=DEMO_USER_ID,reservation_id=order['reservation_id'],idempotency_key='req-cancel',request_id='req-cancel')
        assert verify_history(c)['integrity_status']=='verified'
        assert verify_collateral_invariants(c)['integrity_status']=='verified'


def test_history_verifier_readonly_connection_and_noop_rejects_partial():
    import sqlite3
    from app.v2_balance_history import verify_history
    with IsolatedMigrationFixture() as f:
        assert migrate_fixture(f,with_history=True);c=f.conn
        with sqlite3.connect(f.path.as_uri()+'?mode=ro',uri=True) as ro:
            ro.execute('PRAGMA foreign_keys=ON')
            assert ro.row_factory is None and not ro.in_transaction
            assert verify_history(ro)['integrity_status']=='verified'
            assert ro.row_factory is None and not ro.in_transaction
        c.execute('DROP TRIGGER balance_history_context_delete_guard');c.commit()
        before=list(c.iterdump())
        with pytest.raises(RuntimeError,match='history_invalid'):migrate_fixture(f)
        with pytest.raises(RuntimeError,match='history_invalid'):migrate_fixture(f,with_history=True)
        assert list(c.iterdump())==before


def test_history_rejects_mismatched_bucket_creation_operation(sample_markets,tmp_path):
    import sqlite3
    from app.storage import DEMO_USER_ID
    from app.collateral_ledger import allocate_v2_points_to_participant
    from app.v2_balance_history import verify_history
    with IsolatedMigrationFixture() as f:
        account,_=_history_migration_full(f,sample_markets);c=f.conn
        allocate_v2_points_to_participant(c,participant_id=DEMO_USER_ID,amount_micro=1,idempotency_key='later-allocation')
        path=tmp_path/'wrong-pair.sqlite'
        with sqlite3.connect(path) as out:c.backup(out)
    c=sqlite3.connect(path)
    ddl=c.execute("SELECT sql FROM sqlite_master WHERE name='balance_history_bucket_immutable_update'").fetchone()[0]
    c.execute('DROP TRIGGER balance_history_bucket_immutable_update')
    c.execute("UPDATE balance_history_bucket SET creation_operation_id=(SELECT operation_id FROM balance_history_operation WHERE source_type='point_allocation_event' ORDER BY source_id DESC LIMIT 1) WHERE kind='POINT_LOCKED' AND account_id=?",(account,))
    c.execute(ddl);c.commit();c.execute('PRAGMA foreign_keys=ON')
    assert 'history_bucket_creation_invalid' in verify_history(c)['violation_codes']
    c.close()


@pytest.mark.parametrize('corruption',['legacy_row','sequence'])
def test_history_migration_final_legacy_preservation_gate(sample_markets,corruption):
    from test_v2_clob_migration import populated,snapshot
    with IsolatedMigrationFixture() as f:
        populated(f,sample_markets);c=f.conn;before=snapshot(c)
        def inject(stage):
            if stage=='history_precommit':
                if corruption=='legacy_row': c.execute("UPDATE markets SET payload=payload||' ' WHERE market_id=(SELECT min(market_id) FROM markets)")
                else: c.execute("UPDATE sqlite_sequence SET seq=0 WHERE name='demo_audit_events'")
        expected='legacy_data_changed' if corruption=='legacy_row' else 'legacy_sequence_changed'
        with pytest.raises(RuntimeError,match=expected):migrate_fixture(f,with_history=True,failure_hook=inject)
        assert c.execute('PRAGMA user_version').fetchone()[0]==1
        assert c.execute('PRAGMA foreign_keys').fetchone()[0]==1
        assert snapshot(c)==before
