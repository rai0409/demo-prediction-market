# V2 NORMAL / MINT / MERGE Matching Contract — Phase 3B-3 design

STATUS = DESIGN_ONLY
MATCHING_ENGINE = NOT_IMPLEMENTED
PRODUCTION_MIGRATION = NOT_AUTHORIZED
PUBLIC_RELEASE_READY = false
SCHEMA_CHANGE = NOT_EXECUTED
FEATURE_FLAGS_ENABLED = false

## 1. Authority and compatibility

This document specifies **future** local, simulated, fully collateralized YES/NO CLOB execution for `rai0409/demo-prediction-market`. It builds on `docs/v2-clob-execution-contract.md` and `docs/v2-balance-history-contract.md` at `main` commit `1683908711c6d15e80b9257a9049092df4f9ccb4` (PR #26).

For **matching modes, cross-outcome eligibility, trade-leg cardinality, financial roles, maker-price calculations and match selection**, this document supersedes the **NORMAL-only** assumptions in sections 2.1, 3, 5.2, 6 and acceptance case 9 of `v2-clob-execution-contract.md`. All other existing safety constraints remain in force. The currently committed Python code, SQLite schema version 2 and test fixtures remain **NORMAL-only and unchanged** by the document. Merely approving this design does not authorize running MINT or MERGE against the current schema.

No real funds, crypto, redemption, transfers between V1 and V2, external order routing or real Polymarket trading; Polymarket remains read-only. No fees, leverage, AMM, external market maker, negative balances, or guaranteed liquidity. All balances are integers (`POINT_SCALE = 10000` micro-points per point; whole shares). User-owned collateral fully funds every active order and every outstanding complete set.

## 2. Order representation and three matching modes

An accepted order is `(market_id, participant_id, side BUY|SELL, outcome YES|NO, quantity, limit_price_micro, priority_seq, expires_at_us?)`. Quantity `1..1_000_000` and price `1..10000` inclusive retain the existing admission contract; maximum 1000 fills per order. Each accepted order has exactly one linked version-2 reservation: BUY locks `quantity * limit_price_micro` micro-points; SELL locks `quantity` shares of the specified outcome. One order can be partially filled by different permitted matching modes, but it retains its original identity, limit and priority.

Exactly three modes are permitted:

| Mode | Order A | Order B | Effect |
|---|---|---|---|
| `NORMAL` | BUY YES and SELL YES; or BUY NO and SELL NO | same outcome, opposite sides | Transfer existing shares for micro-points, market reserve and total share supply unchanged |
| `MINT` | BUY YES | BUY NO, same market | Issue `q` YES and `q` NO shares backed by `q*POINT_SCALE` newly locked in market reserve |
| `MERGE` | SELL YES | SELL NO, same market | Destroy `q` YES and `q` NO shares; release `q*POINT_SCALE` from market reserve to sellers |

Never pair YES and NO orders in NORMAL, YES/YES or NO/NO in MINT/MERGE, and never match orders in different markets. `split_complete_sets` and `merge_complete_sets` in `app/collateral_ledger.py` are **existing same-account direct complete-set operations**; CLOB MINT/MERGE are **different two-order executions** and must not be represented as two fabricated direct split/merge operations.

## 3. Price compatibility and price formation

Define one execution per matched pair and quantity `q = min(maker_remaining, taker_remaining)`. Two fill legs record each order's `side`, `outcome`, `quantity` and **executed** price. Each executed price must be `1..10000`, obey its order limit and remain a checked integer.

**NORMAL:** orders have the same outcome and opposite side. A crossing exists if `BUY.limit >= SELL.limit`. Both legs execute at the existing **resting maker's** limit price `P`: `SELL.limit <= P <= BUY.limit`.

**MINT:** two opposite-outcome BUY orders cross when `B_YES+B_NO >= POINT_SCALE`. The **resting maker** BUY leg executes at `P_m = min(B_m, POINT_SCALE - 1)`; the taker opposite-outcome BUY executes at `P_t = POINT_SCALE - P_m`. Admit this candidate only if `1 <= P_t <= B_t` and both executed prices obey their respective limits. Explicit price-10000 boundary rule: a resting BUY at limit 10000 participates at 9999, obtaining a 1-micro-point-per-share refund; no zero-price leg is permitted. This is the only maker-price exception to the existing NORMAL contract. `P_YES+P_NO=POINT_SCALE` exactly.

**MERGE:** two opposite-outcome SELL orders cross when `A_YES+A_NO <= POINT_SCALE`. The resting maker SELL leg executes at its ask `P_m=A_m`, and the taker opposite-outcome SELL leg receives `P_t=POINT_SCALE-P_m`. Admit only if `1 <= P_t <= POINT_SCALE` and `P_t >= A_t`. A maker SELL at 10000 cannot cross any valid opposite-outcome SELL order. `P_YES+P_NO=POINT_SCALE` exactly. Market reserve must already contain the corresponding complete sets.

The order's **limit**, not execution price, determines original BUY collateral and reservation consumption. Price improvement is returned to that buyer in the same transaction, with an explicit zero refund record if the improvement is zero. For MERGE, both sellers receive their corresponding execution-price proceeds; no market-maker subsidy exists.

## 4. Deterministic selection across matching modes

Each incoming order searches **two** eligible resting books in the same market:

- Incoming `BUY X`: NORMAL candidates are `SELL X`, with effective purchase price `maker.ask`; MINT candidates are `BUY opposite(X)`, with effective purchase price `POINT_SCALE - min(maker.bid, POINT_SCALE-1)`. Among all valid nonexpired candidates with effective price `<= incoming.limit`, choose the **lowest effective price**; break ties by **smallest maker `priority_seq`**.
- Incoming `SELL X`: NORMAL candidates are `BUY X`, with effective proceeds price `maker.bid`; MERGE candidates are `SELL opposite(X)`, with effective proceeds price `POINT_SCALE - maker.ask`. Among all valid nonexpired candidates with effective price `>= incoming.limit`, choose the **highest effective price**; break ties by **smallest maker `priority_seq`**.

Do not give arbitrary priority to NORMAL over MINT/MERGE. A matching mode is derived from the actual two orders, never provided as an unchecked client hint. `priority_seq` is globally increasing, allocated in the same `BEGIN IMMEDIATE` transaction as order admission; no client-supplied time, order ID or mode overrides priority. Expire eligible crossed makers in deterministic book order before selecting a live candidate. **Self-trade prevention:** if the chosen highest-priority maker is owned by the same canonical participant (including cross-outcome complement orders), cancel only the incoming unfilled remainder, retain earlier fills and the maker; do not skip that maker. Recheck market state, participant identity, expiry, limits, fill cap and balances under the same lock.

If a candidate cannot satisfy positive execution-price bounds, it is not eligible for the complementary mode; it may still be matched normally if eligible. Partial fills, 1000-fill terminalization, expiry, cancellation, freeze, idempotent replay, crash rollback and existing order statuses follow the base contract. No execution is split into independently committed half-trades.

## 5. Mandatory atomic asset movements

### 5.1 NORMAL (existing five roles, unchanged)

For quantity `q`, BUY limit `B` and executed price `P`:

1. BUY locked points: `-q*B`.
2. BUY available points refund: `+q*(B-P)` (explicit zero role when `B=P`).
3. SELL available points: `+q*P`.
4. SELL locked shares of matched outcome: `-q`.
5. BUY available shares of matched outcome: `+q`.

### 5.2 MINT (seven logical roles)

For BUY YES limit `B_Y`, BUY NO limit `B_N` and executed leg prices `P_Y+P_N=POINT_SCALE`:

1. YES buyer locked points: `-q*B_Y`.
2. YES buyer available points refund: `+q*(B_Y-P_Y)` (record zero if necessary).
3. NO buyer locked points: `-q*B_N`.
4. NO buyer available points refund: `+q*(B_N-P_N)` (record zero if necessary).
5. YES buyer available YES shares: `+q`.
6. NO buyer available NO shares: `+q`.
7. Market reserve points: `+q*POINT_SCALE`; `net_complete_sets += q`.

The treasury `issued_micro` and `burned_micro` **do not change**; the reserve is funded entirely from previously available/locked participant points. All seven logical movements belong to one finalized trade and one ordered balance-history source. Do not count reservation consume-event amounts or history-event deltas a second time.

### 5.3 MERGE (five logical roles)

For SELL YES ask `A_Y`, SELL NO ask `A_N` and executed leg prices `P_Y+P_N=POINT_SCALE`:

1. YES seller locked YES shares: `-q`.
2. NO seller locked NO shares: `-q`.
3. YES seller available points proceeds: `+q*P_Y`.
4. NO seller available points proceeds: `+q*P_N`.
5. Market reserve points: `-q*POINT_SCALE`; `net_complete_sets -= q`.

Both shares are extinguished exactly once, and the reserve is debited exactly once. Do not invoke legacy `merge_complete_sets` separately for the two participants. The two SELL reservations consume `q` locked shares each; there is no point refund on SELL. Both sellers must be distinct canonical participants.

### 5.4 Invariants at every committed boundary

```
issued_micro - burned_micro
    = sum(point_accounts.available_micro + point_accounts.locked_micro)
      + sum(market_reserves.reserve_micro)
for each market:
    market_reserve_micro == net_complete_sets * POINT_SCALE
    total_YES_shares == total_NO_shares == net_complete_sets
for each account:
    locked_micro == sum(active BUY reservation.remaining_collateral)
for each account, market, outcome:
    locked_shares == sum(active SELL reservation.remaining_collateral)
for each accepted reservation:
    original_collateral == remaining_collateral + consumed_collateral + released_collateral
for each order:
    quantity == filled_quantity + unfilled_quantity
    filled_quantity == sum(its fill quantities)
```

Each NORMAL/MINT/MERGE trade has exactly two fills (one for each order), exactly two consumption source records, and its **mode-specific** ledger-role set, including zero refunds. Every trade must reconcile with an ordered, finalized history source and both existing V1/V2 conservation verifiers. Missing, duplicated, unlinked or forged roles fail closed. All intermediate multiplication/addition/subtraction and reserve/set counters use checked signed-64-bit arithmetic; SQLite REAL promotion, negative points/shares and floating inputs are forbidden. A failed transaction leaves every balance, reservation, event, audit record, order, fill and history unchanged.

## 6. Required schema, verifier, history and consumer changes before implementation

The current committed Schema v2 is structurally NORMAL-only: `v2_trades.buy_order_id/sell_order_id`, `v2_fills.UNIQUE(trade_id,side)`, `v2_trade_ledger_entries` five-role enum and `verify_v2()` assume BUY+SELL of one outcome. **It is forbidden to enable MINT or MERGE by relaxing runtime checks without a reviewed schema migration and verifier update.**

The future revision must explicitly model `match_type` (`NORMAL`,`MINT`,`MERGE`), two distinct maker/taker orders, **two individually priced fills** with their own sides and outcomes, trade-leg-specific consume roles, and mode-specific asset/bucket/owner identities. Preserve NORMAL's public behavior and historical data. For MINT/MERGE, same-side fills are required; `UNIQUE(trade_id,side)` must therefore be replaced by a correct per-order uniqueness constraint. Normal-only `buy_order_id`, `sell_order_id`, `outcome` and one-price fields cannot be mandatory for complementary trades; define nullable or versioned replacements and update exact-schema validation accordingly. Reconcile per-trade roles rather than assuming every trade has exactly five fixed roles.

Include market reserve and both outcome share movements in immutable trade-ledger evidence, complete-set count checks, and ordered balance-history capture. Extend reservation consume-event roles without breaking legacy `contract_version=1` reservations. Preserve external reservation cancel/reject responses, actor-scoped idempotency, audit hashes, replay behavior and fail-closed read-only verifiers. `app/storage.py:init_db` remains a V1 entry point until a separately reviewed operational migration. `app/database_backup.py` version-aware backup/restore acceptance remains a separate gate. Existing `/api/admin/v2/point-allocations` and reservation endpoints are not converted into public matching endpoints by this design.

Version policy: the current repository already contains schema version 2, but a persistent V2 deployment has **not** been ruled out by an environment inventory. Do **not** silently rewrite or reuse version 2 for a different schema. Before implementation decide the schema fingerprint/version strategy with an explicit environment inventory; any persistent V2 database requires a reviewed forward migration. Unknown/ambiguous versions fail closed. A new schema version (e.g. 3) is the safe default if existing v2 persistence cannot be disproved. No production database modification is authorized.

Before implementation, repository-wide search for exact table/column/index assertions, schema versions, test fixtures, JSON keys, response codes, importers, backup metadata, scripts and dynamic SQL consumers. Include every necessary production/test/document path in the same scoped implementation change or stop with BLOCKED. In particular inspect `app/v2_clob_schema.py`, `app/v2_clob_migration.py`, `app/v2_clob_invariants.py`, `app/v2_balance_history.py`, `app/collateral_ledger.py`, `app/storage.py`, `app/main.py`, `app/database_backup.py`, `tests/test_v2_clob_schema.py`, `tests/test_v2_clob_invariants.py`, `tests/test_v2_clob_migration.py`, `tests/test_v2_balance_history.py`, `tests/test_collateral_ledger.py`, `tests/test_database_backup.py`, `tests/test_routes.py`, and their discovered consumers.

## 7. Acceptance matrix (required future tests; not executed by this document)

1. NORMAL unchanged: maker price, priorities, five roles, zero refund, V1 isolation and full existing regression suite.
2. MINT: two different BUY participants with no preexisting shares produce one YES and one NO and a fully funded complete set; sum-of-bids boundary `9999` does not cross, `10000` crosses, `10001` crosses with refunds.
3. MINT boundary: resting BUY limit `10000` executes at `9999`, opposite BUY at `1`; no zero-price leg, integer overflow or orphan reserve.
4. MERGE: two SELL participants each with a locked corresponding YES/NO share burn one complete set, release exactly 10000 micro-points, and each receives execution-price proceeds; sum-of-asks `10000` crosses, `10001` does not.
5. Cross-mode candidate selection: BUY selects lowest effective price, SELL selects highest, ties globally break by resting `priority_seq`, regardless of execution mode; no maker skipping.
6. Self-matching prevention across NORMAL, MINT and MERGE; partial prior fills preserved; unauthorized identity/session alias cannot bypass STP.
7. Partial fills and mixed-mode fills against one order, 1000-fill cap, cancellation and expiry return only unused collateral.
8. Two fills per execution, correct per-leg side/outcome/price, consumed reservation amounts, zero refund records, exactly the required mode-specific ledger roles; tampering fails closed.
9. Every pre/post trade conservation and balance-history bucket chain; counter, signed-64-bit and reserve underflow/overflow boundaries.
10. Idempotency conflict/replay, concurrent BUY/BUY, SELL/SELL and BUY/SELL matching, crash/rollback, SQLite busy/full and source-of-truth checks on a separate isolated SQLite fixture.
11. Market frozen/closed forbids admission/fill, preserves cancellation; no V2 settlement or void payout until separately authorized.
12. Schema migration/backup/restore version compatibility; unknown persistent V2 fingerprint rejects without mutation; complete suite unchanged.

## 8. Delivery boundary

This is **contract-only**. It does not add endpoints, create or migrate databases, modify the existing schema, enable runtime feature flags, or implement matching. After review and merge of this contract, the next independently scoped task is **Schema + invariant + balance-history support for three modes**, with complete affected test/consumer permissions and isolated fixtures. The actual matching engine and API wiring follow only after that schema gate passes. Public release, operational migration, user-facing settlement/void and legal go-live are later independent gates.
