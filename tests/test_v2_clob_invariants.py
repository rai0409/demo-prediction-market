import hashlib
import json

import pytest

from app.collateral_ledger import (bootstrap_v2_point_supply, allocate_v2_points_to_participant,
    create_collateral_market, split_complete_sets, verify_collateral_invariants)
from app.storage import store_markets
from app.v2_clob_migration import IsolatedMigrationFixture, migrate_fixture


def insert(c, table, v):
    return c.execute(f"insert into {table} ({','.join(v)}) values ({','.join('?' for _ in v)})",tuple(v.values())).lastrowid


@pytest.fixture
def trade_fixture(sample_markets):
    with IsolatedMigrationFixture() as f:
        c=f.conn
        store_markets(c,sample_markets)
        c.execute("insert into demo_users(user_id,balance) values ('seller',0)")
        c.commit()
        bootstrap_v2_point_supply(c,amount_micro=200000,idempotency_key='supply')
        buyer=allocate_v2_points_to_participant(c,participant_id='participant-1',amount_micro=100000,idempotency_key='buyer')['destination_account_id']
        seller=allocate_v2_points_to_participant(c,participant_id='seller',amount_micro=100000,idempotency_key='seller')['destination_account_id']
        market=sample_markets[0]['market_id']
        create_collateral_market(c,market_id=market)
        split_complete_sets(c,account_id=seller,market_id=market,quantity=10,idempotency_key='split')
        migrate_fixture(f)
        common=dict(engine_key='collateralized_clob_v2',market_id=market,outcome='YES',quantity=10,status='reserved',created_at='x',updated_at='x',contract_version=2,released_collateral=0)
        rbuy=insert(c,'order_collateral_reservations',common|dict(account_id=buyer,participant_id='participant-1',side='BUY',limit_price_micro=6000,collateral_type='point',collateral_amount=60000,remaining_collateral=42000,consumed_collateral=18000))
        rsell=insert(c,'order_collateral_reservations',common|dict(account_id=seller,participant_id='seller',side='SELL',limit_price_micro=5000,collateral_type='share',collateral_amount=10,remaining_collateral=7,consumed_collateral=3))
        for rid,account,asset,amount,before,after,locked,participant in [(rbuy,buyer,'point',60000,100000,40000,60000,'b'),(rsell,seller,'share',10,10,0,10,'s')]:
            event=insert(c,'order_collateral_events',dict(engine_key='collateralized_clob_v2',reservation_id=rid,account_id=account,event_type='reserve',asset_type=asset,asset_amount=amount,available_before=before,available_after=after,locked_before=0,locked_after=locked,idempotency_key='reserve-'+participant,payload_hash='legacy-hash',created_at='x'))
            for bucket,delta,b,a in [('available',-amount,before,after),('locked',amount,0,locked)]:
                insert(c,'order_collateral_ledger_entries',dict(engine_key='collateralized_clob_v2',reservation_id=rid,event_id=event,account_id=account,market_id=market,outcome='YES',asset_type=asset,balance_bucket=bucket,delta=delta,balance_before=b,balance_after=a,created_at='x'))
        base=dict(engine_key='collateralized_clob_v2',market_id=market,outcome='YES',quantity=10,filled_quantity=3,unfilled_quantity=7,fill_count=1,status='PARTIALLY_FILLED',created_at_us=1,updated_at_us=2,version=1)
        ob=insert(c,'v2_orders',base|dict(reservation_id=rbuy,account_id=buyer,participant_id='participant-1',side='BUY',limit_price_micro=6000,priority_seq=2))
        os=insert(c,'v2_orders',base|dict(reservation_id=rsell,account_id=seller,participant_id='seller',side='SELL',limit_price_micro=5000,priority_seq=1))
        trade=insert(c,'v2_trades',dict(engine_key='collateralized_clob_v2',market_id=market,outcome='YES',buy_order_id=ob,sell_order_id=os,maker_order_id=os,taker_order_id=ob,quantity=3,price_micro=5000,execution_key=f'v2:{ob}:1',taker_fill_ordinal=1,created_at_us=2))
        for oid,side in [(ob,'BUY'),(os,'SELL')]: insert(c,'v2_fills',dict(trade_id=trade,order_id=oid,side=side,quantity=3,price_micro=5000,created_at_us=2))
        for role,acc,asset,bucket,delta,b,a in [('BUY_LOCKED',buyer,'point','locked',-18000,60000,42000),('BUY_REFUND',buyer,'point','available',3000,40000,43000),('SELL_PROCEEDS',seller,'point','available',15000,0,15000),('SELL_SHARES',seller,'share','locked',-3,10,7),('BUY_SHARES',buyer,'share','available',3,0,3)]:
            insert(c,'v2_trade_ledger_entries',dict(trade_id=trade,account_id=acc,market_id=market,outcome='YES',role=role,asset_type=asset,bucket=bucket,delta=delta,balance_before=b,balance_after=a,created_at_us=2))
        for rid,acc,role,asset,amount,old,new,available_before,available_after,locked_before,locked_after in [(rbuy,buyer,'BUY_CONSUME','point',18000,60000,42000,40000,43000,60000,42000),(rsell,seller,'SELL_CONSUME','share',3,10,7,0,0,10,7)]:
            payload=dict(version='v2_consume_v1',engine_key='collateralized_clob_v2',reservation_id=rid,trade_id=trade,event_role=role,asset_type=asset,asset_amount=amount,reservation_before=old,reservation_after=new,consumed_before=0,consumed_after=amount)
            digest=hashlib.sha256(json.dumps(payload,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()).hexdigest()
            insert(c,'order_collateral_events',dict(engine_key='collateralized_clob_v2',reservation_id=rid,account_id=acc,event_type='consume',asset_type=asset,asset_amount=amount,available_before=available_before,available_after=available_after,locked_before=locked_before,locked_after=locked_after,idempotency_key=None,payload_hash=digest,created_at='x',trade_id=trade,event_role=role,reservation_before=old,reservation_after=new,consumed_before=0,consumed_after=amount))
        c.execute('update point_accounts set available_micro=43000,locked_micro=42000 where account_id=?',(buyer,))
        c.execute('update point_accounts set available_micro=15000 where account_id=?',(seller,))
        c.execute("update outcome_positions set available_shares=0,locked_shares=7 where account_id=? and market_id=? and outcome='YES'",(seller,market))
        c.execute("insert into outcome_positions(account_id,market_id,outcome,available_shares,locked_shares,updated_at) values (?,?,'YES',3,0,'x')",(buyer,market))
        c.commit()
        yield c


def test_nonempty_trade_is_verified_read_only(trade_fixture):
    c=trade_fixture
    before=c.total_changes
    result=verify_collateral_invariants(c)
    assert result['integrity_status']=='verified',result
    assert c.total_changes==before


@pytest.mark.parametrize('role',['BUY_LOCKED','BUY_REFUND','SELL_PROCEEDS','SELL_SHARES','BUY_SHARES'])
def test_missing_trade_role_detected(trade_fixture,role):
    c=trade_fixture
    c.execute('delete from v2_trade_ledger_entries where role=?',(role,))
    assert 'trade_ledger_mismatch' in verify_collateral_invariants(c)['violation_codes']


@pytest.mark.parametrize('role',['BUY_LOCKED','SELL_PROCEEDS','SELL_SHARES','BUY_SHARES'])
def test_bad_trade_ledger_amount_detected(trade_fixture,role):
    c=trade_fixture
    row=c.execute('select delta,balance_before from v2_trade_ledger_entries where role=?',(role,)).fetchone()
    delta=row['delta']+1 if row['delta']<0 else row['delta']+1
    c.execute('update v2_trade_ledger_entries set delta=?,balance_after=? where role=?',(delta,row['balance_before']+delta,role))
    assert 'trade_ledger_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_missing_consume_detected(trade_fixture):
    trade_fixture.execute("delete from order_collateral_events where event_type='consume' and event_role='BUY_CONSUME'")
    assert verify_collateral_invariants(trade_fixture)['integrity_status']=='failed'


def test_missing_fill_detected(trade_fixture):
    trade_fixture.execute("delete from v2_fills where side='SELL'")
    assert 'trade_fill_mismatch' in verify_collateral_invariants(trade_fixture)['violation_codes']


def test_fill_wrong_quantity_detected(trade_fixture):
    trade_fixture.execute("update v2_fills set quantity=2 where side='SELL'")
    assert 'trade_fill_mismatch' in verify_collateral_invariants(trade_fixture)['violation_codes']


def test_order_reservation_mismatch_detected(trade_fixture):
    trade_fixture.execute("update v2_orders set limit_price_micro=5999 where side='BUY'")
    assert 'order_reservation_mismatch' in verify_collateral_invariants(trade_fixture)['violation_codes']


def test_wrong_hash_detected(trade_fixture):
    c=trade_fixture
    c.execute('drop trigger v2_event_immutable')
    c.execute("update order_collateral_events set payload_hash=? where event_type='consume' and event_role='BUY_CONSUME'",('b'*64,))
    assert 'consume_event_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_orphan_and_overflow_fail_closed(trade_fixture):
    c=trade_fixture
    c.execute("update point_accounts set available_micro=9223372036854775807 where owner_id='participant-1'")
    assert verify_collateral_invariants(c)['integrity_status']=='failed'
    assert 'integer_overflow_or_invalid' in verify_collateral_invariants(c)['violation_codes']


def test_unknown_version_fails_closed(trade_fixture):
    trade_fixture.execute('pragma user_version=3')
    assert 'unsupported_schema_version' in verify_collateral_invariants(trade_fixture)['violation_codes']


def test_zero_refund_still_requires_ledger_row(trade_fixture):
    c=trade_fixture
    c.execute('drop trigger v2_reservation_immutable')
    c.execute('drop trigger v2_event_immutable')
    c.execute("update order_collateral_reservations set limit_price_micro=6000 where side='SELL'")
    c.execute("update v2_orders set limit_price_micro=6000 where side='SELL'")
    c.execute('update v2_trades set price_micro=6000')
    c.execute('update v2_fills set price_micro=6000')
    c.execute("update v2_trade_ledger_entries set delta=0,balance_after=balance_before where role='BUY_REFUND'")
    c.execute("update v2_trade_ledger_entries set delta=18000,balance_after=18000 where role='SELL_PROCEEDS'")
    c.execute("update order_collateral_events set available_after=40000 where event_type='consume' and event_role='BUY_CONSUME'")
    c.execute("update point_accounts set available_micro=40000 where owner_id='participant-1'")
    c.execute("update point_accounts set available_micro=18000 where owner_id='seller'")
    assert verify_collateral_invariants(c)['integrity_status']=='verified'
    c.execute("delete from v2_trade_ledger_entries where role='BUY_REFUND'")
    assert 'trade_ledger_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_remaining_collateral_wrong_fails(trade_fixture):
    c=trade_fixture
    c.execute('pragma ignore_check_constraints=ON')
    c.execute("update order_collateral_reservations set remaining_collateral=41999 where side='BUY'")
    assert 'reservation_accounting_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_consume_quantity_wrong_fails(trade_fixture):
    c=trade_fixture
    c.execute('drop trigger v2_event_immutable')
    c.execute('pragma ignore_check_constraints=ON')
    c.execute("update order_collateral_events set asset_amount=4 where event_type='consume' and event_role='SELL_CONSUME'")
    assert 'consume_event_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_orphan_fill_fails(trade_fixture):
    c=trade_fixture
    c.commit()
    c.execute('pragma foreign_keys=OFF')
    c.execute('update v2_fills set trade_id=999 where side="SELL"')
    assert 'foreign_key_invalid' in verify_collateral_invariants(c)['violation_codes']


def test_wrong_refund_amount_detected(trade_fixture):
    trade_fixture.execute("update v2_trade_ledger_entries set delta=3001,balance_after=40001+3000 where role='BUY_REFUND'")
    assert 'trade_ledger_mismatch' in verify_collateral_invariants(trade_fixture)['violation_codes']


def test_self_trade_or_wrong_owner_detected(trade_fixture):
    c=trade_fixture
    c.execute('drop trigger v2_reservation_immutable')
    c.execute("update order_collateral_reservations set participant_id='participant-1' where side='SELL'")
    c.execute("update v2_orders set participant_id='participant-1' where side='SELL'")
    result=verify_collateral_invariants(c)
    assert 'trade_order_mismatch' in result['violation_codes']
    assert 'order_reservation_mismatch' in result['violation_codes']


def test_negative_balance_detected(trade_fixture):
    c=trade_fixture
    c.execute('pragma ignore_check_constraints=ON')
    c.execute("update point_accounts set available_micro=-1 where owner_id='seller'")
    assert 'negative_balance_detected' in verify_collateral_invariants(c)['violation_codes']


def test_legacy_and_v2_reservations_count_once(trade_fixture):
    from app.collateral_ledger import reserve_v2_order_collateral
    c=trade_fixture
    market=c.execute('select market_id from v2_trades').fetchone()[0]
    reserve_v2_order_collateral(c,participant_id='participant-1',market_id=market,side='BUY',outcome='NO',quantity=1,limit_price_micro=1000,idempotency_key='legacy-reserve')
    assert verify_collateral_invariants(c)['integrity_status']=='verified'
    c.execute("update point_accounts set available_micro=available_micro-1000,locked_micro=locked_micro+1000 where owner_id='participant-1'")
    assert 'buy_locked_points_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_consume_snapshot_must_match_trade_ledger(trade_fixture):
    c=trade_fixture
    c.execute('drop trigger v2_event_immutable')
    c.execute("update order_collateral_events set locked_after=6 where event_type='consume' and event_role='SELL_CONSUME'")
    assert 'consume_event_mismatch' in verify_collateral_invariants(c)['violation_codes']


def test_v1_overflow_returns_failed_instead_of_sqlite_sum_error(db_conn):
    db_conn.execute("insert into point_accounts(account_id,engine_key,owner_type,owner_id,available_micro,created_at,updated_at) values ('one','collateralized_clob_v2','participant','one',9223372036854775807,'x','x')")
    db_conn.execute("insert into point_accounts(account_id,engine_key,owner_type,owner_id,available_micro,created_at,updated_at) values ('two','collateralized_clob_v2','participant','two',1,'x','x')")
    result=verify_collateral_invariants(db_conn)
    assert result['integrity_status']=='failed'
    assert 'integer_overflow_or_invalid' in result['violation_codes']
