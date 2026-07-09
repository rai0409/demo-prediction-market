# API

## GET /

Purpose: Render the dashboard with filtered displayable markets.

Response: HTML.

Notes: Default view hides closed, inactive, expired, zero-liquidity, and resolved-looking markets.

## GET /markets/{market_id}

Purpose: Render one market detail page.

Response: HTML.

Notes: Shows the `予想する` panel only when demo participation is allowed.

## GET /demo-positions

Purpose: Render local demo positions, `予想履歴`, and demo point ledger.

Response: HTML.

Safety note: These records are local simulation records only.

## GET /demo-results

Purpose: Render local demo result tracking rows.

Response: HTML.

Notes: Includes a `結果を確認する` action that performs conservative local-only demo settlement through `POST /api/demo/settle`.

## GET /health

Purpose: Basic app health check.

Response shape:

```json
{"ok": true, "title": "Demo Prediction Market Viewer"}
```

## POST /api/refresh

Purpose: Fetch live/sample markets, store current markets and snapshots, and return fetch metadata.

Response shape summary:

- `status`
- `error`
- `raw_count`
- `normalized_count`
- `fallback_used`
- `markets`
- `count`

## GET /api/markets

Purpose: Return default filtered displayable markets.

Response shape summary:

- `markets`
- `count`
- `total_market_count`
- `displayable_market_count`
- hidden-count fields
- `filters_applied`
- `latest_filter_run_at`

Each market includes display fields such as `display_status`, `display_reasons`, `demo_participation_allowed`, and `demo_participation_block_reason`.

When public WebSocket observations exist, each market also includes:

- `realtime_status`: `ws_live`, `ws_stale`, or `rest_only`
- `ws_last_event_at`
- `best_bid`
- `best_ask`
- `last_trade_price`
- `realtime_spread`

## GET /api/markets?include_all=true

Purpose: Return all stored normalized markets, including hidden/archived rows.

Notes: Useful for diagnostics and comparing filtered count to total count.

## GET /api/markets/{market_id}

Purpose: Return one enriched market.

Response shape summary: normalized market fields plus display/eligibility fields.

## GET /api/markets/{market_id}/snapshots

Purpose: Return recent stored snapshots for one market.

Response shape summary:

- `snapshots`: list of fetched timestamp plus market payload.

## GET /api/debug/source-status

Purpose: Return current source, fallback, runtime diagnostic, and filtering metadata.

Response shape summary:

- `live_enabled`
- configured values
- last fetch status/error/time/url/http status
- raw and normalized counts
- fallback flag
- total/displayable/hidden counts
- runtime diagnostic file existence flags

Safety note: No secrets are exposed because the app does not use secrets.

## GET /api/realtime/status

Purpose: Return optional public market update status.

Response shape summary:

- `ws_enabled`
- `ws_top_n`
- `ws_stale_seconds`
- `latest_update_at`
- `update_count`
- `live_market_update_count`
- `stale_market_update_count`
- `rest_only_count`

Notes:

- WebSocket is disabled by default.
- The app continues to work through REST/sample fallback when no WebSocket observations exist.
- No private credentials are exposed or required.

## GET /api/demo/balance

Purpose: Return local demo user balance.

Response shape summary:

- `user_id`
- `balance`

## GET /api/demo/wallet

Purpose: Return local demo point management state.

Response shape summary:

- `user_id`
- `balance`
- `ledger`
- `audit_events`
- `summary`

Summary fields:

- `total_added`
- `total_used_for_demo_participation`
- `total_settled`
- `total_adjusted`
- `ledger_count`

## POST /api/demo/wallet/add-points

Purpose: Add local demo points for simulation.

Request shape:

```json
{"amount": 1000, "reason": "デモポイント追加", "idempotency_key": "optional-key"}
```

Notes:

- Amount is clamped to the accepted range `1..100000`.
- Creates a `demo_point_add` ledger row with balance-before/balance-after metadata.
- Creates a local audit event.
- Repeated idempotency keys replay the previous local result.

## POST /api/demo/wallet/reset

Purpose: Reset local demo balance to the default starting balance.

Request shape:

```json
{"reason": "デモ残高リセット", "idempotency_key": "optional-key"}
```

Notes:

- Creates a `demo_balance_reset` ledger row.
- Creates a local audit event.
- Repeated idempotency keys replay the previous local result.

## GET /api/demo/positions

Purpose: Return local simulation state.

Response shape summary:

- `balance`
- `positions`
- `orders`
- `ledger`

## GET /api/demo/results

Purpose: Return local pending/result tracking rows.

Response shape summary:

- `balance`
- `results`
- `pending_count`
- `settled_count`

Notes: Result rows include market title/question when available, selected outcome, stake, probability, estimated return, status, winning outcome, payout, settlement source/note, created time, and settled time.

## GET /api/demo/resolution-candidates

Purpose: Return public WebSocket `market_resolved` observations that are treated as local result candidates.

Response shape summary:

- `candidate_count`
- `markets_with_candidates_count`
- `candidates`
- `latest_by_market`
- `pending_settlement_market_ids`
- `generated_at`

Notes:

- Candidates are not enough for local demo settlement.
- `POST /api/demo/settle` still requires REST/conservative confirmation.
- Raw event JSON is omitted from this endpoint.

## POST /api/demo/settle

Purpose: Check pending local demo result rows against stored public market data and settle only when the winning outcome is clear.

Response shape summary:

- `checked_count`
- `settled_win_count`
- `settled_loss_count`
- `pending_count`
- `unknown_count`
- `total_payout`
- `balance`
- `ws_candidate_count`
- `ws_confirmed_count`
- `ws_unconfirmed_count`
- `ws_conflict_count`
- `rest_only_settled_count`

Notes:

- Uses explicit winning outcome fields when available.
- Uses a strict probability fallback only for closed/resolved markets with exactly one probability `>= 0.999` and all others `<= 0.001`.
- Uses WebSocket `market_resolved` observations only as candidates.
- Does not settle from WebSocket-only candidates.
- Blocks settlement when WebSocket and REST disagree.
- Does not infer a final result from closed/inactive/end-date signals alone.
- Repeated calls do not double-pay.
- Local demo settlement only; no external order or money movement occurs.

## POST /api/demo/predict

Purpose: Create a local-only simulated prediction record.

Request shape:

```json
{"market_id": "sample-market-tokyo-rain", "outcome": "YES", "stake": 100, "idempotency_key": "optional-key"}
```

Response shape summary:

- `balance`
- `position`
- `message`

Safety notes:

- Validates market exists.
- Validates outcome exists.
- Validates numeric positive stake.
- Validates sufficient demo balance.
- Validates demo participation eligibility server-side.
- Optional idempotency key prevents duplicate local deduction for replayed requests.
- Never places a real order.
- Never calls external trading APIs.
