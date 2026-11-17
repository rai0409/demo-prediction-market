"""Read-only reconciliation of V2 order and trade records."""
from __future__ import annotations
import hashlib
import json
from collections import defaultdict

MAX = 9223372036854775807
ENGINE = 'collateralized_clob_v2'
SCALE = 10000


def verify_v2(conn, market_id=None):
    codes = []
    def fail(code):
        if code not in codes:
            codes.append(code)
    def integer(x):
        if type(x) is not int or x < 0 or x > MAX:
            fail('integer_overflow_or_invalid')
            return 0
        return x
    def total(items):
        result = sum(integer(v) for v in items)
        if result > MAX:
            fail('integer_overflow_or_invalid')
        return result
    def rows(table):
        return [dict(row) for row in conn.execute('select * from '+table)]
    state = conn.execute('select issued_micro,burned_micro from point_supply_state where engine_key=?',(ENGINE,)).fetchone()
    if state is None:
        return {'integrity_status':'failed','engine_key':ENGINE,'market_count':0,'issued_micro':0,'burned_micro':0,'account_micro':0,'reserve_micro':0,'violation_count':1,'violation_codes':['engine_unavailable']}
    issued,burned = map(integer,state)
    accounts = {r['account_id']:r for r in rows('point_accounts') if r['engine_key']==ENGINE}
    positions = {(r['account_id'],r['market_id'],r['outcome']):r for r in rows('outcome_positions')}
    markets = {r['market_id']:r for r in rows('collateral_markets') if r['engine_key']==ENGINE}
    reserves = {r['market_id']:r for r in rows('market_reserves')}
    reservations = {r['id']:r for r in rows('order_collateral_reservations')}
    events = rows('order_collateral_events'); locks = rows('order_collateral_ledger_entries')
    orders = {r['id']:r for r in rows('v2_orders')}; trades = {r['id']:r for r in rows('v2_trades')}
    fills = rows('v2_fills'); ledger = rows('v2_trade_ledger_entries')
    if any(v < 0 for a in accounts.values() for v in (a['available_micro'],a['locked_micro'])) or any(v < 0 for p in positions.values() for v in (p['available_shares'],p['locked_shares'])) or any(r['reserve_micro'] < 0 or r['net_complete_sets'] < 0 for r in reserves.values()): fail('negative_balance_detected')
    numeric = {'point_accounts':('available_micro','locked_micro','version'), 'outcome_positions':('available_shares','locked_shares','version'), 'order_collateral_reservations':('quantity','limit_price_micro','collateral_amount','remaining_collateral','consumed_collateral','released_collateral','version'), 'order_collateral_events':('asset_amount','available_before','available_after','locked_before','locked_after'), 'v2_orders':('quantity','filled_quantity','unfilled_quantity','fill_count','limit_price_micro','priority_seq','created_at_us','updated_at_us','version'), 'v2_trades':('quantity','price_micro','taker_fill_ordinal','created_at_us'), 'v2_fills':('quantity','price_micro','created_at_us'), 'v2_trade_ledger_entries':('balance_before','balance_after','created_at_us')}
    for table, columns in numeric.items():
        for row in rows(table):
            for col in columns: integer(row[col])
    for row in reserves.values():
        integer(row['reserve_micro']); integer(row['net_complete_sets']); integer(row['version'])
    for row in ledger:
        if type(row['delta']) is not int or not -MAX <= row['delta'] <= MAX: fail('integer_overflow_or_invalid')
    for event in events:
        if event['event_type']=='consume':
            for col in ('reservation_before','reservation_after','consumed_before','consumed_after'): integer(event[col])
    if conn.execute('pragma foreign_key_check').fetchone() is not None: fail('foreign_key_invalid')
    account_micro=total(v for a in accounts.values() for v in (a['available_micro'],a['locked_micro']))
    reserve_micro=total(reserves[m]['reserve_micro'] for m in markets if m in reserves)
    if issued-burned != account_micro+reserve_micro: fail('global_point_conservation_failed')
    selected=[m for m in markets if not market_id or market_id==m]
    for m in selected:
        r=reserves.get(m)
        if r is None: fail('market_reserve_missing'); continue
        n=integer(r['net_complete_sets']); amount=integer(r['reserve_micro'])
        if n*SCALE>MAX: fail('integer_overflow_or_invalid')
        if amount!=n*SCALE: fail('reserve_complete_set_mismatch')
        for outcome,code in [('YES','yes_supply_mismatch'),('NO','no_supply_mismatch')]:
            supply=total(v for (a,market,o),p in positions.items() if market==m and o==outcome for v in (p['available_shares'],p['locked_shares']))
            if supply!=n: fail(code)
    event_by_res=defaultdict(list); lock_by_event=defaultdict(list); fill_by_order=defaultdict(list); fill_by_trade=defaultdict(list); ledger_by_trade=defaultdict(list); linked=defaultdict(list)
    for e in events: event_by_res[e['reservation_id']].append(e)
    for l in locks: lock_by_event[l['event_id']].append(l)
    for f in fills: fill_by_order[f['order_id']].append(f); fill_by_trade[f['trade_id']].append(f)
    for l in ledger: ledger_by_trade[l['trade_id']].append(l)
    for o in orders.values(): linked[o['reservation_id']].append(o)
    for a in accounts.values():
        remaining=total(r['remaining_collateral'] for r in reservations.values() if r['account_id']==a['account_id'] and r['side']=='BUY')
        if integer(a['locked_micro'])!=remaining: fail('buy_locked_points_mismatch')
    for k,p in positions.items():
        remaining=total(r['remaining_collateral'] for r in reservations.values() if (r['account_id'],r['market_id'],r['outcome'])==k and r['side']=='SELL')
        if integer(p['locked_shares'])!=remaining: fail('sell_locked_shares_mismatch')
    for r in reservations.values():
        owner=accounts.get(r['account_id'])
        if owner is None or owner['owner_type']!='participant' or owner['owner_id']!=r['participant_id'] or r['engine_key']!=ENGINE or r['market_id'] not in markets:
            fail('order_reservation_mismatch')
        if r['side']=='BUY' and r['account_id'] not in accounts: fail('buy_locked_points_mismatch')
        if r['side']=='SELL' and r['remaining_collateral'] and (r['account_id'],r['market_id'],r['outcome']) not in positions: fail('sell_locked_shares_mismatch')
    for r in reservations.values():
        orig=integer(r['collateral_amount']); rem=integer(r['remaining_collateral']); cons=integer(r['consumed_collateral']); rel=integer(r['released_collateral'])
        quantity=integer(r['quantity']); price=integer(r['limit_price_micro'])
        if quantity<1 or not 1<=price<=SCALE or (r['side']=='BUY' and (r['collateral_type']!='point' or quantity*price!=orig or quantity*price>MAX)) or (r['side']=='SELL' and (r['collateral_type']!='share' or quantity!=orig)) or r['side'] not in ('BUY','SELL') or r['outcome'] not in ('YES','NO'):
            fail('reservation_accounting_mismatch')
        if rem+cons+rel!=orig or rem+cons+rel>MAX: fail('reservation_accounting_mismatch')
        es=event_by_res[r['id']]; reserve=[e for e in es if e['event_type']=='reserve']; release=[e for e in es if e['event_type']=='release']; consumes=[e for e in es if e['event_type']=='consume']
        if len(reserve)!=1 or len(release)!=(1 if rel else 0) or total(e['asset_amount'] for e in consumes)!=cons or total(e['asset_amount'] for e in release)!=rel: fail('reservation_event_state_mismatch')
        if reserve and reserve[0]['asset_amount']!=orig: fail('reservation_event_state_mismatch')
        if len(consumes) != len({e['trade_id'] for e in consumes}): fail('consume_event_mismatch')
        counter=0
        for e in sorted(consumes,key=lambda item:item['id']):
            if e['consumed_before']!=counter or e['reservation_before']!=orig-counter:
                fail('consume_event_mismatch')
            counter+=integer(e['asset_amount'])
            if e['consumed_after']!=counter or e['reservation_after']!=orig-counter:
                fail('consume_event_mismatch')
        if r['contract_version']==1:
            if consumes or cons or r['consumed_at'] is not None or (r['status']=='reserved' and (rem!=orig or rel)) or (r['status']=='released' and (rem or rel!=orig or r['release_reason'] not in ('cancelled','rejected'))) or r['status'] not in ('reserved','released') or linked[r['id']]: fail('reservation_event_state_mismatch')
        elif r['contract_version']==2:
            if len(linked[r['id']])!=1: fail('order_reservation_mismatch')
        else: fail('reservation_accounting_mismatch')
        if (r['status']=='reserved' and (rem==0 or r['release_reason'] is not None or r['released_at'] is not None or r['consumed_at'] is not None)) or (r['status']=='released' and (rem!=0 or rel==0 or r['released_at'] is None or r['release_reason'] not in ('cancelled','rejected','expired','market_frozen','self_trade_prevention','fill_limit') or r['consumed_at'] is not None)) or (r['status']=='consumed' and (rem!=0 or rel!=0 or r['consumed_at'] is None or r['released_at'] is not None or r['release_reason'] is not None)) or r['status'] not in ('reserved','released','consumed'): fail('reservation_accounting_mismatch')
    for e in events:
        r=reservations.get(e['reservation_id']); ls=lock_by_event[e['id']]
        if not r or e['account_id']!=r['account_id'] or e['engine_key']!=r['engine_key'] or e['asset_type']!=r['collateral_type']: fail('order_collateral_ledger_mismatch'); continue
        if e['event_type']=='consume':
            if ls or e['idempotency_key'] is not None: fail('consume_event_mismatch')
            data={k:e[k] for k in ('engine_key','reservation_id','trade_id','event_role','asset_type','asset_amount','reservation_before','reservation_after','consumed_before','consumed_after')}
            data['version']='v2_consume_v1'
            digest=hashlib.sha256(json.dumps(data,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()
            if digest!=e['payload_hash'] or e['reservation_before']-e['reservation_after']!=e['asset_amount'] or e['consumed_after']-e['consumed_before']!=e['asset_amount']: fail('consume_event_mismatch')
        else:
            amount=integer(e['asset_amount'])
            if e['idempotency_key'] is None or (e['event_type']=='reserve' and (e['available_before']-e['available_after']!=amount or e['locked_after']-e['locked_before']!=amount or e['release_reason'] is not None)) or (e['event_type']=='release' and (e['available_after']-e['available_before']!=amount or e['locked_before']-e['locked_after']!=amount or e['release_reason']!=r['release_reason'])) or e['event_type'] not in ('reserve','release'): fail('reservation_event_state_mismatch')
            expected={'available':(e['available_before'],e['available_after']),'locked':(e['locked_before'],e['locked_after'])}
            if len(ls)!=2 or {l['balance_bucket'] for l in ls}!=set(expected): fail('order_collateral_ledger_mismatch'); continue
            for l in ls:
                b,a=expected[l['balance_bucket']]
                if (l['engine_key']!=e['engine_key'] or l['reservation_id']!=r['id'] or l['account_id']!=r['account_id'] or l['market_id']!=r['market_id'] or l['outcome']!=r['outcome'] or l['asset_type']!=e['asset_type'] or l['balance_before']!=b or l['balance_after']!=a or l['delta']!=a-b): fail('order_collateral_ledger_mismatch')
    for o in orders.values():
        r=reservations.get(o['reservation_id']); fs=fill_by_order[o['id']]
        if not r or r['contract_version']!=2 or any(o[k]!=r[k] for k in ('engine_key','account_id','participant_id','market_id','outcome','side','quantity','limit_price_micro')): fail('order_reservation_mismatch'); continue
        if o['filled_quantity']!=total(f['quantity'] for f in fs) or o['fill_count']!=len(fs) or o['quantity']!=o['filled_quantity']+o['unfilled_quantity']: fail('order_fill_mismatch')
        q=o['quantity']; f=o['filled_quantity']; u=o['unfilled_quantity']; s=o['status']
        if not 1<=q<=1000000 or not 1<=o['limit_price_micro']<=SCALE or not 0<=o['fill_count']<=1000 or o['priority_seq']<1: fail('order_fill_mismatch')
        if not ((s=='OPEN' and f==0 and u>0) or (s=='PARTIALLY_FILLED' and f>0 and u>0) or (s=='FILLED' and f==q and u==0) or (s in ('CANCELLED','EXPIRED') and u>0) or (s=='REJECTED' and f==0 and u==q)): fail('order_fill_mismatch')
        reason=o['terminal_reason']
        if (s in ('OPEN','PARTIALLY_FILLED') and reason is not None) or (s=='FILLED' and reason!='fully_filled') or (s=='CANCELLED' and reason not in ('cancelled','self_trade_prevention','market_frozen','fill_limit')) or (s=='EXPIRED' and reason!='expired') or (s=='REJECTED' and reason!='rejected'): fail('order_fill_mismatch')
        rem=o['unfilled_quantity']*(o['limit_price_micro'] if o['side']=='BUY' else 1) if o['status'] in ('OPEN','PARTIALLY_FILLED') else 0
        if rem>MAX: fail('integer_overflow_or_invalid')
        if r['remaining_collateral']!=rem: fail('order_reservation_mismatch')
    for t in trades.values():
        buy=orders.get(t['buy_order_id']); sell=orders.get(t['sell_order_id']); q=integer(t['quantity']); price=integer(t['price_micro'])
        if not buy or not sell: fail('trade_order_mismatch'); continue
        if q<1 or not 1<=price<=SCALE or not 1<=integer(t['taker_fill_ordinal'])<=1000: fail('trade_order_mismatch')
        if t['engine_key']!=ENGINE or buy['side']!='BUY' or sell['side']!='SELL' or buy['participant_id']==sell['participant_id'] or buy['account_id']==sell['account_id'] or any(o['market_id']!=t['market_id'] or o['outcome']!=t['outcome'] for o in (buy,sell)) or not sell['limit_price_micro']<=price<=buy['limit_price_micro'] or {t['maker_order_id'],t['taker_order_id']}!={buy['id'],sell['id']} or t['execution_key']!=f"v2:{t['taker_order_id']}:{t['taker_fill_ordinal']}": fail('trade_order_mismatch')
        maker=orders.get(t['maker_order_id']); taker=orders.get(t['taker_order_id'])
        if not maker or not taker or maker['limit_price_micro']!=price or maker['priority_seq']>=taker['priority_seq']: fail('trade_order_mismatch')
        fs=fill_by_trade[t['id']]
        if len(fs)!=2 or {(f['order_id'],f['side'],f['quantity'],f['price_micro']) for f in fs}!={(buy['id'],'BUY',q,price),(sell['id'],'SELL',q,price)}: fail('trade_fill_mismatch')
        expected={'BUY_LOCKED':(buy['account_id'],'point','locked',-q*buy['limit_price_micro']), 'BUY_REFUND':(buy['account_id'],'point','available',q*(buy['limit_price_micro']-price)), 'SELL_PROCEEDS':(sell['account_id'],'point','available',q*price), 'SELL_SHARES':(sell['account_id'],'share','locked',-q), 'BUY_SHARES':(buy['account_id'],'share','available',q)}
        ls=ledger_by_trade[t['id']]
        if len(ls)!=5 or {l['role'] for l in ls}!=set(expected): fail('trade_ledger_mismatch')
        for l in ls:
            if l['role'] not in expected: fail('trade_ledger_mismatch'); continue
            owner,asset,bucket,delta=expected[l['role']]
            if abs(delta)>MAX: fail('integer_overflow_or_invalid')
            if (l['account_id'],l['asset_type'],l['bucket'],l['delta'])!=(owner,asset,bucket,delta) or l['market_id']!=t['market_id'] or l['outcome']!=t['outcome'] or integer(l['balance_before'])+l['delta']!=integer(l['balance_after']): fail('trade_ledger_mismatch')
        cs=[e for e in events if e['event_type']=='consume' and e['trade_id']==t['id']]
        expected_cs={(buy['reservation_id'],'BUY_CONSUME',q*buy['limit_price_micro']),(sell['reservation_id'],'SELL_CONSUME',q)}
        if len(cs)!=2 or {(e['reservation_id'],e['event_role'],e['asset_amount']) for e in cs}!=expected_cs: fail('consume_event_mismatch')
        role_rows={l['role']:l for l in ls}
        for e in cs:
            if e['event_role']=='BUY_CONSUME':
                av=role_rows.get('BUY_REFUND'); lk=role_rows.get('BUY_LOCKED')
            else:
                av=None; lk=role_rows.get('SELL_SHARES')
            if not lk or (e['locked_before'],e['locked_after'])!=(lk['balance_before'],lk['balance_after']) or (av and (e['available_before'],e['available_after'])!=(av['balance_before'],av['balance_after'])) or (not av and e['available_before']!=e['available_after']): fail('consume_event_mismatch')
    if any(e['reservation_id'] not in reservations for e in events) or any(l['event_id'] not in {e['id'] for e in events} for l in locks) or any(f['order_id'] not in orders or f['trade_id'] not in trades for f in fills) or any(l['trade_id'] not in trades for l in ledger): fail('foreign_key_invalid')
    if conn.execute("SELECT 1 FROM sqlite_master WHERE name LIKE 'balance_history_%' LIMIT 1").fetchone() is not None:
        from app.v2_balance_history import verify_history
        history=verify_history(conn)
        for code in history['violation_codes']: fail(code)
    return {'integrity_status':'failed' if codes else 'verified','engine_key':ENGINE,'market_count':len(selected),'issued_micro':issued,'burned_micro':burned,'account_micro':account_micro,'reserve_micro':reserve_micro,'violation_count':len(codes),'violation_codes':codes}
