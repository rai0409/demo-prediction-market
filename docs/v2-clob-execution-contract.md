# V2 CLOB Execution Contract — Phase 3B-1

STATUS = DESIGN_ONLY
PRODUCTION_MIGRATION = NOT_EXECUTED
MATCHING_ENGINE = NOT_IMPLEMENTED
PUBLIC_RELEASE_READY = false

## 1. Scope and authority

This document specifies a future **local, simulated, fully collateralized** YES/NO central limit order book (CLOB). It is a design contract, not evidence that any functionality exists or is approved for production. Baseline: GitHub main `73771b6bfb1fe85c615378b407ed0f0587eafead`. Phase 3B-0's analysis is an input; decisions below supersede its provisional recommendations. `POINT_SCALE = 10000` micro-points per point, and shares are whole integers. There is no real money, redemption, exchange, wallet, external execution or Polymarket order submission. Polymarket access remains read-only. V1 fixed-odds APIs, records, balances, IDs and audit history remain unchanged; no transfers between V1 and V2 are permitted.

Existing `order_collateral_reservations` records original collateral with `reserved`/`released` only; event types are reserve/release and the verifier assumes whole-amount locks and releases. Existing reservation and cancellation endpoints are live contracts. Existing V2 market states include `draft`, `open`, `frozen`, `closed`, `resolved`, `void`; no V2 resolution or void payout is specified here. References: `app/storage.py` (`V1_SCHEMA_SQL`, `init_db`); `app/collateral_ledger.py` (`reserve_v2_order_collateral`, `_release_v2_order_collateral`, `verify_collateral_invariants`); `app/main.py` V2 reservation routes; `app/database_backup.py`; `tests/test_collateral_ledger.py`, `test_collateral_migration.py`, `test_database_backup.py`.

## 2. Final decisions

### 2.1 Book, limits and admission

Only BUY versus SELL of the same market and same outcome match; no YES/NO synthetic crossing, market orders, amendments, fees, fractional shares or external liquidity. Price is an integer from 1 to 10000 inclusive. Each side is sorted by best price (BUY descending, SELL ascending), then ascending `priority_seq`. An accepted order receives a globally unique, increasing, never reused integer sequence inside the write transaction; sequence allocation and order insertion commit together. Wall clock and IDs supplied by a client never decide priority. If the sequence is exhausted, reject admission without asset changes.

A crossing occurs when buy limit >= sell limit. The resting maker's limit is the execution price; incoming order is taker. Each trade quantity is the minimum remaining quantity of these orders. A matching transaction processes the incoming order to completion or until no eligible maker remains. A same-participant crossing cancels the **incoming remainder**, preserving preceding fills and the resting order; it cannot skip that maker to reach lower priority. Identity is the canonical participant ID underlying the authenticated V2 account, not session or client identity. Future beneficial-owner rules are outside this contract.

Maximum original quantity is **1,000,000 shares** per order. Maximum fills per order is **1,000** across its lifetime; one trade creates one fill per order. Once the 1,000th fill commits, any remainder of that order is immediately CANCELLED with reason `fill_limit`, its remaining collateral is returned in the same transaction, and matching continues only if the incoming order still has capacity. In particular, a maker reaching its cap is cancelled before selection of another maker; if the taker reaches its cap its remainder is cancelled and processing stops. An order fully filled on its 1,000th fill is FILLED, not cancelled. An incoming order can therefore create at most 1,000 trades in one transaction. Capacity is checked before every trade. There is no continuation that changes priority or leaves a crossing incoming order unprocessed. A busy/timeout or failure rolls back the entire admission transaction, so client retries use the same idempotency key. The limits are admission contracts, not proven SQLite throughput targets: measure p95/p99 transaction duration, lock wait/busy rate, and rollback behavior at 1,000 fills using isolated concurrent databases before enabling matching. A change to either limit is a versioned contract change requiring new boundary, overflow, stress and recovery tests; the implementation must not silently truncate matching work or split one admitted command into separately committed chunks.

Admission requires engine available, market open, active participant, valid inputs, available collateral and expiry strictly later than server `now`. Expiry is optional; timestamps are UTC integer microseconds in signed 64-bit range. `now >= expires_at` means expired. Before choosing a maker, expire eligible price-crossing candidates in book order and release their remainder, then select the next eligible maker. No matching of an expired order is allowed even if a cleanup worker is late. Admission can reject before creating an order; such failures are not stored as REJECTED orders or replayable command successes.

### 2.2 State machine

`Q=quantity`, `F=filled_quantity`, `U=unfilled_quantity=Q-F`; terminal orders retain U as historical unfilled quantity, even though their live remaining collateral is zero. `C` is remaining collateral, `B` is BUY limit. Order and reservation are linked one-to-one for accepted execution orders. Legacy reservation-only rows remain unlinked and never enter the book.

| From | Trigger | To | Quantity and collateral after transition |
|---|---|---|---|
| admission | reserved; no fill | OPEN | F=0, U=Q>0; C=U×B BUY or U SELL |
| OPEN | first partial fill | PARTIALLY_FILLED | 0<F<Q; C=U×B or U |
| OPEN / PARTIALLY_FILLED | final fill | FILLED | F=Q, U=0, C=0 |
| PARTIALLY_FILLED | further partial fill | PARTIALLY_FILLED | 0<F<Q; C=U×B or U |
| OPEN / PARTIALLY_FILLED | owner cancellation, STP, freeze or fill cap | CANCELLED | U>0, C=0; only C before transition returned |
| OPEN / PARTIALLY_FILLED | expiry | EXPIRED | U>0, C=0; only C before transition returned |
| OPEN | system rejection after acceptance and before any fill | REJECTED | F=0, U=Q, C=0; full C returned |

All other transitions are forbidden, including PARTIALLY_FILLED→REJECTED and every transition out of a terminal state. A post-admission refusal after any fill is CANCELLED. For the creation command, admission, matching, and any immediate terminal transition commit as one unit; OPEN is a logical state even when not externally observed. Cancel of an already terminal order with a fresh key returns `order_not_open` without change; same-key committed cancellation replays its original response. Owner cancellation is allowed while market is not open, so locked assets can always be released. Administrator rejection is allowed only for an OPEN linked order with F=0; otherwise it fails closed. A legacy unlinked reservation keeps the old API behavior. FILLED uses `terminal_reason='fully_filled'`; CANCELLED, EXPIRED and REJECTED require a permitted cause code. Logical admission, possible fills and immediate terminalization remain a single committed command; intermediate states are not separately observable.

### 2.3 Market lifecycle

Only `open` admits or matches. A freeze transaction first changes market status to `frozen` under BEGIN IMMEDIATE, making subsequent admission/fill impossible; it may cancel all open orders atomically or process bounded batches after freeze. Batches release remaining collateral and set CANCELLED with `market_frozen`; repeated batches are idempotent. Closing, resolving or voiding requires zero active orders and zero order collateral in that market, verified under the write lock. Freeze is not automatically undone; reopening requires a separate authorized lifecycle operation, and cancelled orders never revive. Resolution/void payout and changes to complete-set invariants are **not authorized or designed by this contract**; implementation must block V2 payout until a separate reviewed contract exists. No network call occurs within a financial transaction.

## 3. Accounting and conservation

For a trade of q shares, BUY limit B and maker price P, all integer micro-points: `1 <= P <= B <= 10000` and P >= SELL limit. Apply simultaneously:

| Ledger role | Bucket | Delta |
|---|---|---:|
| BUY_LOCKED | buyer locked points | −q×B |
| BUY_REFUND | buyer available points | +q×(B−P) |
| SELL_PROCEEDS | seller available points | +q×P |
| SELL_SHARES | seller locked shares for outcome | −q |
| BUY_SHARES | buyer available shares for outcome | +q |

All five roles exist **exactly once** per trade, including a BUY_REFUND row with delta 0 and before=after when P=B. Other roles are nonzero. A trade ledger row describes the actual bucket mutation; it is the authoritative monetary trade entry, not another mutation to be replayed on top of balance updates. Reservation-consumption events record the associated reservation amounts and link to trade, but do not independently mutate buckets or duplicate trade ledger deltas. Existing order-collateral ledger rows continue to represent actual reserve/release bucket transfers; trade rows are not written there. A consumer must not sum event amounts and ledger deltas as independent balance movements.

Example: BUY 10 YES @6000 reserves 60000. SELL has 10 YES locked. A maker SELL @5000 fills 3: buyer locked −18000, buyer available +3000, seller available +15000, seller locked YES −3, buyer available YES +3. BUY C becomes 42000; SELL C becomes 7. Cancelling BUY returns 42000 from locked to available; F=3, U=7, state CANCELLED. Seller's residual 7 stays locked unless separately cancelled. If P=6000, BUY_REFUND is an explicit zero row.

For each reservation, `original_collateral = remaining_collateral + consumed_collateral + released_collateral`; BUY consumed is q×B, comprising q×P seller proceeds plus q×(B−P) buyer refund. SELL consumed is q. Legacy rows are classified separately but included in the same aggregate exactly once. Global and per-market invariants before V2 resolution/void:

```text
issued_micro − burned_micro = Σ(account.available_micro + account.locked_micro) + Σ(market.reserve_micro)
market.reserve_micro = market.net_complete_sets × 10000
Σ(YES available + YES locked) = Σ(NO available + NO locked) = market.net_complete_sets
account.locked_micro = Σ remaining_collateral of that account's reserved BUY reservations
position.locked_shares = Σ remaining_collateral of that account/market/outcome's reserved SELL reservations
trade point deltas sum to 0; trade outcome share deltas sum to 0
order.quantity = order.filled_quantity + order.unfilled_quantity
order.filled_quantity = Σ its fill quantities; each trade has exactly one BUY and one SELL fill
```

Each balance and counter must be integer, nonnegative and <= `9223372036854775807`. Reject booleans, non-integers and noncanonical inputs. Check each multiplication before computing (`a <= MAX//b`), each positive addition (`x <= MAX−delta`), each subtraction (`x >= magnitude`), sequence increment, and all version increments. SQLite expressions must never overflow into REAL: evaluate checked arithmetic in application integer arithmetic before binding, and guard SQL predicates; do not depend on SQLite multiplication in CHECK as overflow detection. Verifier sums use checked arbitrary-precision accumulation, fail closed on out-of-range intermediate or final totals, and never silently coerce to float. Zero is allowed only for BUY_REFUND ledger role. No negative balances or uncollateralized order are permitted.

## 4. Schema strategy decision

| Criterion | A: extend existing reservation model | B: preserve old reservation tables and add a separate new-order collateral model |
|---|---|---|
| Existing reservation API | Linked rows need dispatch to order-aware cancel/reject; unlinked rows retain behavior | Old API is unchanged only if new collateral is never represented as an old reservation; otherwise old full release is unsafe |
| Coexistence | One reservation namespace; legacy rows remain unlinked | Two lock sources and two lifecycles; explicit discriminator required |
| Locked reconciliation | Sum each reservation's remaining amount once | Must union disjoint old and new sources; overlap creates double counting or orphan locks |
| IDs and audit | Preserve existing IDs, old rows/events and hashes through controlled rebuild | Old IDs untouched, but new namespace and cross-ledger relationships add complexity |
| FK/CHECK | Rebuild constrained tables and verify FK, old semantics and cross-row rules | Additive tables simplify initial migration, but do not enforce 1:1 with the existing reservation without changing its legacy behavior |
| Verifier | Extend one source of truth, while testing old event rules | Two separate lock and event accounting paths must be reconciled |
| Migration/rollback | Higher schema rebuild and downtime risk; requires exclusive cutover and verified snapshot | Lower initial DDL risk, but downgrade still unsafe after new trades |
| Backup/restore | New schema/version and updated verifier needed; old binary cannot interpret it | Same version/verifier/restore compatibility issue despite additive DDL |
| Operational/data loss | Controlled migration and restore rehearsal essential; post-trade restore can lose history | Fewer rewritten old rows, but dual-accounting and rollback mismatch risks persist |

**Choose A.** It satisfies the required one-to-one relationship with `order_collateral_reservations`, avoids two authoritative lock sources, and makes legacy versus linked reservation semantics explicit. Its rebuild risk is accepted only with offline fixtures, verified backups, writer exclusion and compatibility gates. Neither option permits old and new writers to run simultaneously after migration.

## 5. Complete logical schema

This is normative logical schema, not executable SQL. All PKs, FKs and UNIQUE constraints are enforced by SQLite with foreign keys enabled on every connection. Integer columns require `typeof(column)='integer'`, range checks and explicit NOT NULL unless marked nullable. Text IDs are nonempty; timestamps are UTC integer microseconds for new tables. Existing legacy columns, storage types, IDs and old event semantics are preserved. New amount/count fields use checked arithmetic per section 3; CHECK definitions involving products must be guarded to avoid overflow. `engine_key` is exactly `collateralized_clob_v2`. Immutable history is append-only; no UPDATE/DELETE of committed trade, fill, ledger, command result, or audit rows by application paths.

### 5.1 Reservation and event extensions

- `order_collateral_reservations`: retain **all current columns and constraints for legacy fields**, original `collateral_amount` immutable. Add `contract_version INTEGER NOT NULL` (1 legacy, 2 execution), `remaining_collateral`, `consumed_collateral`, `released_collateral` integers >=0. Original equals their sum. `contract_version=1` has no linked order and retains old `reserved`/`released` event behavior. Version 2 has exactly one order before commit. Status values: `reserved` iff C>0, `released` iff C=0 and released_collateral>0, `consumed` iff C=0 and released_collateral=0. `release_reason` is NULL while reserved or consumed; released reason is one of `cancelled`, `rejected`, `expired`, `market_frozen`, `self_trade_prevention`, `fill_limit`. `released_at` is set iff status=released; `consumed_at` is set iff consumed. A terminal partially filled reservation with any returned amount is released, not consumed. Existing version-1 CHECK semantics remain conditionally enforced; widening CHECK constraints without a `contract_version` discriminator is forbidden. Specifically, `contract_version=1` permits only the original statuses (`reserved`/`released`), original release reasons (`cancelled`/`rejected`), and original timestamp relationship; its new accounting columns are derived solely from original status and amount. Version 2 permits `reserved`/`released`/`consumed` with the documented reason and timestamp rules. Add `consumed_at` as nullable; the other timestamps preserve their original storage formats, with no rewrite of legacy values. `remaining_collateral + consumed_collateral + released_collateral = collateral_amount` must be integer-overflow safe; verify via checked integer arithmetic rather than SQLite arithmetic that can promote to REAL. Account, participant, market, outcome, side, quantity and limit are immutable and must agree with linked order.
- `order_collateral_events`: preserve all existing columns, IDs and reserve/release semantics, keys and historical rows. Extend event_type with `consume`; add nullable `trade_id` FK and `event_role` (`BUY_CONSUME`, `SELL_CONSUME` for consume), unique `(trade_id,event_role)` with NULL allowed only for non-consume. Consume asset_amount is q×B for BUY or q for SELL; it records reservation remaining/consumed before/after in additional integer columns `reservation_before`, `reservation_after`, `consumed_before`, `consumed_after`. For consume, existing available/locked before/after fields record actual account bucket states at the point after the trade ledger mutations; they are snapshots only and do not assert an internal transfer. They must match the corresponding ledger roles; reserve/release fields retain existing transfer equations. For reserve/release trade_id and event_role are NULL. A nonzero residual release has one new release event with actual residual amount and the normal two order-collateral ledger entries. Full fill does not fabricate a release event. Event unique `(engine_key,account_id,idempotency_key)` remains for external reserve/release requests; consume events use a NULL idempotency key and are uniquely identified by `(trade_id,event_role)`; legacy and external reserve/release event keys remain non-NULL, preserving existing keys without assuming a previously unused prefix. This REQUIRES rebuilding the existing `idempotency_key TEXT NOT NULL` column into a nullable column with an explicit conditional CHECK: `event_type='consume'` iff `idempotency_key IS NULL`, and `event_type IN ('reserve','release')` iff `idempotency_key IS NOT NULL`. For consume, `trade_id`, `event_role`, the before/after counters and an immutable `payload_hash` are mandatory; for reserve/release, these consume-only fields are NULL. Compute consume `payload_hash` as SHA-256 of canonical UTF-8 JSON with a fixed `v2_consume_v1` version marker, `engine_key`, `reservation_id`, `trade_id`, `event_role`, `asset_type`, `asset_amount` and checked before/after counters; sort keys with compact separators, never include non-deterministic request IDs. Old payload hashes are preserved byte-for-byte. Preserve `UNIQUE(engine_key,account_id,idempotency_key)` for non-NULL keys; SQLite permits multiple NULLs, so `(trade_id,event_role)` must independently be UNIQUE and NULL-restricted for consume. Preserve legacy uniqueness, event IDs and audit hashes. `consume` does not create any `order_collateral_ledger_entries` rows; reserve/release still create exactly two. The verifier checks these type-specific counts and must not apply the old two-row rule to consume.
- `order_collateral_ledger_entries`: preserve columns, primary and foreign keys, existing nonzero delta and `UNIQUE(event_id,balance_bucket)`. Exactly two opposite entries per reserve/release event; zero entries for consume events, whose actual asset movements appear in `v2_trade_ledger_entries`. Extend verifier by event type, never weaken old checks.

### 5.2 New tables

- `v2_orders`: `id INTEGER PRIMARY KEY`, `reservation_id INTEGER NOT NULL UNIQUE REFERENCES order_collateral_reservations(id)`, `engine_key TEXT NOT NULL`, `account_id TEXT NOT NULL REFERENCES point_accounts(account_id)`, `participant_id TEXT NOT NULL`, `market_id TEXT NOT NULL REFERENCES collateral_markets(market_id)`, `side TEXT CHECK IN ('BUY','SELL')`, `outcome TEXT CHECK IN ('YES','NO')`, `quantity INTEGER CHECK BETWEEN 1 AND 1000000`, `filled_quantity`, `unfilled_quantity` nonnegative with checked sum = quantity, `fill_count INTEGER CHECK BETWEEN 0 AND 1000`, `limit_price_micro INTEGER CHECK BETWEEN 1 AND 10000`, `status TEXT` in six named states, `terminal_reason TEXT NULL`, `priority_seq INTEGER NOT NULL UNIQUE CHECK >0`, `expires_at_us INTEGER NULL`, `created_at_us`, `updated_at_us`, `version INTEGER >=0`. OPEN iff F=0 and U>0; PARTIALLY_FILLED iff F>0 and U>0; FILLED iff F=Q and U=0; CANCELLED/EXPIRED iff U>0; REJECTED iff F=0 and U=Q. Active orders have null terminal reason; terminal states require a reason. Version 2 reservation's matching attributes and live/terminal collateral equality are validated before commit and by verifier; account, engine, participant, side, quantity, market, outcome and price cannot change. `priority_seq` is assigned by `v2_priority_sequence` (`id` PK CHECK id=1, `last_value` nonnegative integer); increment with guarded UPDATE under the same transaction. No sequence row is deleted or reset.
- `v2_trades`: `id INTEGER PRIMARY KEY`, `engine_key`, `market_id REFERENCES collateral_markets`, `outcome`, `buy_order_id REFERENCES v2_orders`, `sell_order_id REFERENCES v2_orders`, `maker_order_id REFERENCES v2_orders`, `taker_order_id REFERENCES v2_orders`, `quantity INTEGER >0`, `price_micro INTEGER BETWEEN 1 AND 10000`, `execution_key TEXT NOT NULL UNIQUE`, `created_at_us`. CHECK distinct buy/sell, maker/taker, and maker/taker must be the two counterpart order IDs. Both order sides, market/outcome, distinct participant, price, limits, maker age, quantities and creation timestamp are cross-row validated under lock and reconciled by verifier. `execution_key` is server-generated `v2:<taker_order_id>:<taker_fill_ordinal>`; ordinal is positive and unique for that taker. It is not client supplied. `UNIQUE(taker_order_id,taker_fill_ordinal)`; include `taker_fill_ordinal INTEGER NOT NULL CHECK BETWEEN 1 AND 1000`.
- `v2_fills`: `id INTEGER PRIMARY KEY`, `trade_id REFERENCES v2_trades NOT NULL`, `order_id REFERENCES v2_orders NOT NULL`, `side TEXT CHECK IN ('BUY','SELL')`, `quantity INTEGER >0`, `price_micro INTEGER BETWEEN 1 AND 10000`, `created_at_us`; `UNIQUE(trade_id,order_id)` and `UNIQUE(trade_id,side)`. Exactly two fills per trade, matching trade's two orders, quantity and price, are mandatory at commit and verification.
- `v2_trade_ledger_entries`: `id INTEGER PRIMARY KEY`, `trade_id REFERENCES v2_trades NOT NULL`, `account_id REFERENCES point_accounts NOT NULL`, `market_id REFERENCES collateral_markets NOT NULL`, `outcome TEXT NOT NULL`, `role TEXT NOT NULL` in the five roles above, `asset_type TEXT CHECK IN ('point','share')`, `bucket TEXT CHECK IN ('available','locked')`, `delta INTEGER`, `balance_before INTEGER >=0`, `balance_after INTEGER >=0`, `created_at_us`; `UNIQUE(trade_id,role)`, checked `before + delta = after`, zero only for BUY_REFUND. Role determines owner, asset and bucket exactly as in section 3. Both point and share rows carry the trade outcome for linkage. A buyer with no prior position gets a zero position created in the same transaction before BUY_SHARES. Verify precisely five rows and both consumption events per trade.
- `v2_command_results`: `id INTEGER PRIMARY KEY`, `engine_key TEXT`, `actor_id TEXT`, `operation TEXT CHECK IN ('place','cancel','reject')`, `idempotency_key TEXT`, `payload_hash TEXT`, `response_status INTEGER`, `response_json TEXT`, `created_at_us INTEGER`; all NOT NULL, `UNIQUE(engine_key,actor_id,operation,idempotency_key)`. Hash is 64 lowercase SHA-256 hex characters. `response_json` contains the immutable committed response including order ID, resulting state at command completion and trade IDs; no secret. One row per committed command. Expiry/freeze internal operations use their own deterministic event keys, not client commands.

Indexes: `(market_id,outcome,side,limit_price_micro,priority_seq)` partial for status OPEN/PARTIALLY_FILLED, with side-specific order clauses/index direction for best-price queries; `(status,expires_at_us,priority_seq)` for expiry; `v2_fills(order_id,id)`; `v2_trades(market_id,outcome,id)`; `v2_trade_ledger_entries(account_id,market_id,id)`; reservation `(account_id,status,side)` and `(account_id,market_id,outcome,status,side)`; event `(reservation_id,id)` and `(trade_id,event_role)`. Unique constraints create their own indexes. Exact SQLite partial-index query plans must be validated, not assumed. All tables, columns, checks, unique keys and indexes are included in schema version validation and backup compatibility checks.

Enforce cross-row constraints with transactions plus verifier and, where practical, constraints/triggers. At commit, exactly one order per version-2 reservation and vice versa; version-1 reservation has none. Foreign keys alone cannot express both directions or exact child counts. All linked reservation cancellation entry points dispatch to the same order transition; direct old full-amount release is forbidden. V2 legacy reserve endpoints still create version-1 unlinked reservations; they are not implicitly converted or matched. For the existing endpoints `/api/v2/order-collateral/reservations/{reservation_id}/cancel` and `/api/admin/v2/order-collateral/reservations/{reservation_id}/reject`, legacy unlinked reservations retain all existing semantics and the original response keys (`reservation_id`, `market_id`, `side`, `outcome`, `quantity`, `limit_price_micro`, `collateral_type`, `collateral_amount`, `status`, `release_reason`, `idempotent_replay`). For linked active reservations, dispatch to the order-aware transition within the same write transaction and return those same keys with `collateral_amount` still the ORIGINAL reserved amount, `status='released'` and `release_reason='cancelled'` or `'rejected'` for permitted operations. Expose the actual returned remainder only in an additive `released_collateral` field, and expose current order state through the new authenticated order GET endpoint. Do not return a historically false released amount by mapping `collateral_amount` to the remainder. A linked FILLED/consumed or otherwise terminal reservation cannot be released by the old endpoint: the new key receives the original `reservation_not_reserved`-class failure with no writes; a previously committed key replays its stored result. Where linked reservations cannot preserve existing response and error behavior, keep new order admission disabled pending an explicit versioned API decision and tests. Every cancel/reject path must prohibit the old full-collateral release for `contract_version=2`.

## 6. Commands, atomicity and replay

Actor is the server-authenticated canonical participant ID. For the currently implemented shared-admin-token boundary, an admin operation uses the distinct technical principal `admin:legacy` (NOT a verified natural-person identity); store the authorized action and request evidence in the audit trail, and do not infer an individual's identity from the shared token. Before multiple administrators or public privileged commands are supported, require individual authenticated administrator IDs, role-based permission checks, actor-scoped idempotency keys and actor-specific audit attribution, with separate security review. Do not introduce or imply RBAC/MFA in this design-only change. Namespace keys by `(engine,actor,operation,key)`. Key is 1–128 printable ASCII characters; reserve internal prefix `sys:` and reject it from clients. Hash canonical UTF-8 JSON: fixed versioned field list, sorted keys, compact separators, Unicode preserved, no NaN; IDs normalized using the existing canonical participant/market rules, enum uppercase, numeric values strict integers, expiry represented as UTC microseconds or null, and authenticated actor plus operation included. Client request IDs are not part of payload hash. Unknown fields are rejected; canonicalization is versioned `v1` and stable. The new command namespace is distinct from legacy reservation idempotency keys. For calls through the existing reservation cancellation endpoint, both the legacy `(engine,account,key)` event namespace and the new linked-command `(engine,actor,operation,key)` namespace must be resolved deterministically under the same write transaction; linked-command replays cannot bypass the old endpoint's authorization or replay a different user's reservation. Existing legacy keys and payload hashes are preserved. A key collision between these namespaces must fail closed rather than reissue or reapply an asset movement.

Within the write lock, same key/hash returns the exact stored status and response body, even if the order has since changed; same key/different hash returns conflict without writes. Current state is retrieved by a separate authenticated GET order API. Failures before a committed command are not cached, including input, authorization, insufficient collateral, busy and rollback failures. A committed command adds its audit events once in the same transaction. The canonical response is immutable: a duplicate command returns its original status/body even after the order is subsequently filled or cancelled. The separate authenticated GET order endpoint is the authority for current state. For an admin operation authenticated only by the existing shared token, use the technical actor described above, and never claim per-administrator attribution. A replay adds no financial or audit event; request-level nonfinancial telemetry may count replay without adding a chained financial audit row. A response lost after commit is recovered by retrying the same key and canonical payload.

```text
place(actor, request):
  validate basic shape; require clean connection
  BEGIN IMMEDIATE
  find command result; replay or conflict if present
  check engine, participant, current market=open, expiry, input bounds, invariants
  allocate priority; reserve exact collateral; insert reservation, order, reserve event,
    two transfer ledger entries and audit
  while incoming is active with remaining > 0:
    expire crossed candidates at server now and release their remainder
    choose best eligible opposite maker by price then priority
    if none: break
    if same participant: cancel incoming remainder (STP); break
    recheck market, expiry, reservation, limits, capacity and arithmetic
    insert unique trade, two fills, five actual-bucket trade ledger entries,
      two consumption events; update both accounts, both positions,
      both reservations, both orders and audit
    terminalize FILLED orders; cancel a still-unfilled order at its 1000th fill
  verify all invariants, old and new; persist immutable response
  COMMIT; return response
on any exception: ROLLBACK all writes and propagate a non-success response
```

`cancel` and `reject` likewise require clean connection and BEGIN IMMEDIATE, check replay first, reread ownership/state/remaining collateral, release exactly that amount once, append two transfer ledger rows and one audit event, check invariants, store response and commit. The same serialization covers competing maker fills, market freeze and expiry. Lock winner determines ordering; loser rereads committed state and cannot double apply. Conditional version/balance updates and uniqueness constraints are defense in depth. No network calls or sleeps while holding the transaction. No nested savepoint substitutes for BEGIN IMMEDIATE; reject an already active connection rather than assuming its lock. `SQLITE_BUSY` is retryable with the same key after rollback. A trade and all its balance, event, order and audit rows either commit together or do not exist.

## 7. Migration, flags and recovery

Migration is a separate implementation, not performed by this document. Use a new explicit schema version and a dedicated clean migration connection; block all old writers and old binaries by an independently verified operational gate BEFORE attempting any table rebuild. `BEGIN IMMEDIATE` serializes a cooperating SQLite writer but is not by itself a deployment-wide writer-exclusion mechanism. If the writer-exclusion gate cannot be established, abort without modifying data. Preflight checks schema version, integrity, FK, audit chain, old invariant verifier and capacity. SQLite backup API produces a transactionally consistent pre-migration snapshot with metadata, hash and a restore-to-separate-path rehearsal. Do not copy a live main DB file alone as a WAL-consistent backup. Rebuild affected CHECK-constrained tables with explicit original primary IDs, exact legacy columns and event payloads, FK/index recreation, and preserve AUTOINCREMENT high-water marks. The migration implementation must execute the following gated sequence on an isolated fixture before any real deployment: (1) capture canonical `sqlite_master` DDL, original table/column/index/trigger/FK definitions, `sqlite_sequence` high-water marks, all existing row IDs, event payload hashes and audit-chain final hash; (2) choose a concrete shadow-table replacement dependency order for `order_collateral_ledger_entries` (child), `order_collateral_events` (child/parent), and `order_collateral_reservations` (parent), including references from OTHER tables; (3) on a clean migration connection, configure `PRAGMA foreign_keys=OFF` BEFORE `BEGIN IMMEDIATE` (changing it inside a transaction is ineffective), assert its value is 0, and require the external writer-exclusion gate; (4) create uniquely named shadow tables with the final intended constraints and FK target names, copy every original row with explicit column lists and original IDs, and backfill only newly introduced columns; (5) drop/replace old tables in a child-safe order, rename shadows to the canonical names in the verified SQLite version, and recreate all explicit indexes, unique constraints and triggers without changing legacy IDs or historical content; (6) restore/check each AUTOINCREMENT high-water mark so a future ID cannot reuse a formerly allocated ID, including gaps and previously deleted rows; (7) under the same transaction, compare row contents via explicit legacy-column projections, event and audit hashes, counts, original IDs, `sqlite_master` final FK target SQL, `PRAGMA foreign_key_check`, `PRAGMA integrity_check`, old and new invariant verifiers, and `user_version`; (8) COMMIT only if every check passes, otherwise ROLLBACK; (9) re-enable `PRAGMA foreign_keys=ON` only OUTSIDE the transaction and assert ON, then repeat foreign-key verification. Do not use naïve `ALTER TABLE old RENAME TO backup` patterns that may rewrite child references to the backup name; run the exact intended DDL/rename/drop sequence on fixture matrices and inspect final FK targets, since SQLite rename behavior varies by version. `PRAGMA defer_foreign_keys` is not a substitute for this tested rebuild procedure. No general migration is claimed to be validated by this specification. Backfill v1 reserved rows as `(remaining=original, consumed=0, released=0)`, released as `(0,0,original)`; no order is fabricated. Compare old IDs, contents, audit final hash, balances and counts; run `foreign_key_check`, integrity check, old and new semantic checks before committing the new version. Failure rolls back the entire migration. Repeated initialization is a no-op for current version; unknown future versions fail closed. Update `CURRENT_SCHEMA_REQUIRED_*`, versioned schema validation and backup metadata/verifier in the same deployable change.

Fixture plan uses only fresh temporary SQLite databases: empty, representative V1-only, current version with no reservations, legacy reserved/released BUY and SELL, noncontiguous IDs, audit chain, multiple accounts and markets, and malformed FK/check/audit/locks. Test repeated migration, interruption at every rebuild stage, checksum and row equality, non-reuse of IDs, version rejection, disabled foreign keys, and legacy endpoints. Never use existing SQLite files. Extend backup tests with pre- and post-migration snapshots, partially filled/cancelled orders, ledger equality, metadata/hash mismatch, corrupt data, and isolated restore. Old-schema restore is validated by its matching version-specific verifier before migration; new-schema restore requires a new-aware binary. Backups taken while writes occur must verify source snapshot consistency rather than comparing independently timed source counts. Specifically, the current `create_backup()` reads `_details(source)` before `source_conn.backup()` and `_sha256(source_path)` separately; these reads do not by themselves establish that the live source and backup represent the same WAL-consistent snapshot. For a new-version backup, verify the *backup database itself* for integrity, schema, audit, invariants and content hash under one consistent snapshot; any source comparison must use a pinned SQLite read transaction or explicit verified write quiescence. Continue to reject restoring a backup to the production DB path. No current backup implementation is assumed to satisfy these enhanced conditions without tests. Preserve WAL consistency and test concurrent backup behavior.

Feature gates are server-side, default false: `v2_clob_admission_enabled` and `v2_clob_matching_enabled`. Schema migration alone enables neither. First deploy migration plus invariant/backup changes with both off; then validate a disposable environment, enable admission and matching together for an explicitly controlled local test environment, then separately decide broader enablement. Admission MUST NOT accept an order while matching is disabled: the two gates must both be true. Turning either off blocks new placement and matching, but cancellation, expiration, freeze and verification remain enabled. Existing legacy reservation endpoints remain usable subject to their existing contract. Gate changes and lifecycle operations are serialized with placement at the application control boundary; if distributed consistency cannot be guaranteed, stop writers before changing flags. No production enablement is authorized here.

After crash before commit, rollback restores pre-command state. After commit, response replay returns stored result without applying trade again. Startup runs integrity, FK, audit and extended accounting checks; discrepancy stops new writes and is investigated, never automatically repaired. For a migration failure before commit use transaction rollback. Before any new-schema writes, a verified isolated restore of the pre-migration snapshot may support rollback under an approved operational procedure. After new writes/trades, restoring an old snapshot discards committed history and is prohibited as an automatic downgrade; preserve current DB, backups and audit, isolate writes and use a separately reviewed forward-repair or explicitly authorized recovery plan. This document authorizes neither production migration nor production restoration.

## 8. Acceptance tests

These are required future tests (65 cases including the original 52), not claims of execution. All use temporary isolated data; no skipped existing contracts. Cases 53–65 are additional implementation acceptance gates for the review findings.

1. BUY reserves exact quantity × limit.
2. SELL reserves exact same-outcome shares.
3. BUY insufficient points leaves no writes.
4. SELL insufficient shares leaves no writes.
5. Zero, negative, float, bool and out-of-range price rejected.
6. Zero, negative, float, bool and >1,000,000 quantity rejected.
7. Overflow on multiplication, addition, counter, sequence and aggregate fails closed.
8. Different markets never cross.
9. YES never crosses NO.
10. Noncrossing limits rest.
11. Best SELL price wins for incoming BUY.
12. Best BUY price wins for incoming SELL.
13. Equal price uses priority sequence, including equal timestamps.
14. Maker price is used from either side.
15. Same participant cancels incoming remainder and never self-trades.
16. STP preserves earlier fills and does not bypass best self maker.
17. Partial BUY retains remaining × limit collateral.
18. Partial SELL retains remaining share collateral.
19. Price improvement refund and seller proceeds have exact integer amounts.
20. Equal maker and BUY price writes exactly one zero BUY_REFUND row.
21. Full fill marks both FILLED and both reserves consumed.
22. Multiple makers create exactly two fills and five ledger roles per trade.
23. Partial cancellation returns only remaining collateral and retains historical U.
24. Cancellation of a filled order makes no asset changes.
25. Fresh-key terminal cancellation conflicts; same-key replay returns original response.
26. Same key and same canonical payload never duplicate a command.
27. Same key and different payload conflicts; different operation/actor scope is distinct.
28. Replay after response loss returns original snapshot; GET returns current state.
29. Replay never appends a second financial audit event.
30. Two simultaneous takers cannot overfill a maker.
31. Fill versus cancel produces one valid serialization with no double release.
32. Freeze versus fill prevents all fills ordered after freeze.
33. Expiry boundary `now == expires_at` rejects fill and releases remainder.
34. Disabled gates reject placement; cancellation remains possible.
35. Both gates on do not admit non-open market orders.
36. The 1000th partial maker fill cancels maker remainder and continues safely.
37. The 1000th partial taker fill cancels taker remainder and stops.
38. Full fill on 1000th fill remains FILLED.
39. Each write-stage injected failure rolls back balances, orders, fills, events and audit.
40. Duplicate execution key, trade role, fill side or reservation link is rejected.
41. Missing, extra, misdirected, wrong-amount or tampered trade rows fail verification.
42. Legacy reserved and released rows retain IDs, balances, events and audit hashes after migration.
43. Legacy and new reservations coexist without duplicate or orphan lock sums.
44. Existing legacy reservation cancel/reject retain old semantics; linked routes use remainder.
45. Unknown schema version and malformed FK/audit/invariants fail migration closed.
46. Interrupted migration rolls back, repeated migration is safe, IDs and sequences preserved.
47. Pre- and post-migration backup/restore validates metadata, schema, audit, locks and all trade records.
48. Concurrent backup yields one consistent snapshot or fails closed.
49. Issued points, reserves, YES/NO shares and trade delta sums reconcile after mixed operations.
50. V1 API fixtures and V1 ledger are unchanged; no network or Polymarket trading calls occur.
51. Expiry/freeze/cancel races leave no active order in a closed market and no leaked lock.
52. Existing linked V2 response fields and separate GET authorization satisfy the compatibility gate.
53. Consume event accepts NULL idempotency key but legacy reserve/release events reject NULL; external unique keys remain unique and trade/role uniqueness prevents duplicate consumption.
54. Consume event payload hash is canonical/deterministic and its trade counters and snapshots reconcile with the five trade-ledger roles; no old two-row transfer ledger entries are created for consume.
55. Migration preserves every original AUTOINCREMENT high-water mark despite deleted IDs, and future insertions cannot reuse a previously allocated identifier.
56. Shadow-table migration proves correct final foreign-key target names across the SQLite versions supported by this repo; rollback at each rebuild stage restores exact old DDL and content.
57. A migration with foreign keys toggled inside a transaction, with a preexisting transaction or without exclusive writer gating is rejected before changing schema.
58. Partially filled linked-order cancellation through the existing route returns the original response fields, original `collateral_amount`, additive actual `released_collateral`, and never releases consumed collateral.
59. Existing unlinked reservations retain their original cancellation/rejection responses, idempotency hashes, row IDs, and event types; linked filled/consumed reservations fail closed under fresh keys.
60. Version-2 consumed reservations and version-1 reservations each satisfy their separate CHECK, release-reason and timestamp rules; malformed cross-version rows are rejected.
61. Admin shared-token actions are labeled as one technical principal rather than a verified individual; later actor-scoped RBAC identities cannot collide with it.
62. A command with 1,000 fills is measured under isolated concurrent load for duration, SQLITE_BUSY behavior, rollback and invariant-verifier cost; a lower safe operational cap must be chosen if limits are exceeded.
63. Version-specific backups pass integrity, foreign key, audit and collateral checks on the saved snapshot; concurrent writes do not cause false mismatches due to differently timed source and backup reads.
64. `execution_key` uses `v2:` and is deterministic from taker order/ordinal; duplicate or V1-namespaced identifiers cannot be created.
65. Committed replay through either old reservation-cancel or new command routes does not change positions, locked collateral, event counts or audit history.

## 9. Delivery and acceptance gates

- **3B-2:** schema version migration, chosen reservation/event extension, verifier, backup/restore and legacy compatibility fixtures; both feature flags off. No matching. The old external cancel/reject routes must continue to work for unlinked legacy reservations. The migration is strictly offline/isolated until separately authorized.
- **3B-3:** command idempotency, clean transaction entry, reservation-backed order lifecycle and cancel/expiry/freeze; gates off until matching is ready.
- **3B-4:** matching, trade/fill/ledger accounting, bounded priority, concurrency and failure injection; isolated flag-controlled testing.
- **3B-5:** end-to-end lifecycle, versioned GET/API compatibility, migration/restore and load/concurrency validation, independent high-risk review and operational rollout proposal. V2 resolution/void payout remains a separate contract.

Every PR must preserve existing tests and run relevant narrow then broader gates. Merge readiness requires successful offline migration/restore rehearsals, invariant/tamper/concurrency tests, audit verification, API compatibility, no production connection, and independent review. Schema downgrade after committed V2 trades is not an acceptance criterion. No implementation, production migration, public release, commit or deployment is authorized by this specification.
