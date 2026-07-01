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

## GET /api/demo/balance

Purpose: Return local demo user balance.

Response shape summary:

- `user_id`
- `balance`

## GET /api/demo/positions

Purpose: Return local simulation state.

Response shape summary:

- `balance`
- `positions`
- `orders`
- `ledger`

## POST /api/demo/predict

Purpose: Create a local-only simulated prediction record.

Request shape:

```json
{"market_id": "sample-market-tokyo-rain", "outcome": "YES", "stake": 100}
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
- Never places a real order.
- Never calls external trading APIs.
