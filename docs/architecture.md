# Architecture

## Overview

Demo Prediction Market Viewer is a local FastAPI product validation app. It fetches public Polymarket-style market data, normalizes the data, persists snapshots in SQLite, filters active/displayable markets, and allows local-only demo participation with simulation-only demo points.

The app is intentionally not a trading app. It has no wallet connection, no private credentials, and no order placement.

## Components

- `app/main.py`: FastAPI app, HTML routes, JSON API routes, template filters, and response shaping.
- `app/config.py`: environment parsing for live mode, polling interval, fetch limit, and SQLite path.
- `app/polymarket_gamma.py`: public Gamma API fetch, raw diagnostics, JSON parsing, defensive normalization, sample fallback loading.
- `app/realtime.py`: live-first refresh orchestration, fallback selection, source debug metadata.
- `app/market_display.py`: market classification, default dashboard filtering, hidden counts, demo participation eligibility.
- `app/demo_points.py`: local demo prediction validation and simulated order/position/ledger creation.
- `app/storage.py`: SQLite schema creation and read/write helpers.
- `app/templates/` and `app/static/`: Jinja pages, polling JavaScript, and CSS.
- `scripts/`: manual verification helpers.
- `tests/`: offline pytest coverage.

## Request Flow

1. `GET /` ensures markets exist in SQLite.
2. Stored markets are classified by `app/market_display.py`.
3. Displayable markets are rendered as dashboard cards.
4. Browser JavaScript polls `GET /api/markets` for update/status metadata.

## Live Fetch Flow

1. `DEMO_PREDICTION_LIVE=1` enables live mode.
2. `app/polymarket_gamma.py` requests the public Gamma events endpoint.
3. JSON is written to `runtime/gamma_last_response.json` when available.
4. Events and nested markets are normalized.
5. Normalized markets are stored in SQLite through `app/storage.py`.
6. Runtime status metadata is written to `runtime/gamma_last_status.json`.

## Sample Fallback Flow

Sample mode is the default. It loads `data/sample_events.json`, normalizes it with the same code path, and stores it in SQLite. If live mode fails or returns no displayable normalized markets, sample data is used with an explicit fallback status.

## Market Filtering Flow

`classify_market_for_display()` checks whether a market is active, not closed, not expired, has outcomes/probabilities, has liquidity, and does not look resolved. `filtered_market_response()` returns default displayable markets plus metadata such as hidden closed, inactive, expired, no-liquidity, and resolved-probability counts.

## Demo Participation Flow

1. User opens a market detail page.
2. UI renders the `予想する` panel only when `demo_participation_allowed` is true.
3. `POST /api/demo/predict` performs server-side checks again.
4. If valid, local demo points are subtracted.
5. Simulated order, simulated position, and ledger rows are stored.
6. No external API call is made for participation.

## Storage Model

SQLite tables:

- `markets`
- `market_snapshots`
- `fetch_runs`
- `demo_users`
- `demo_point_ledger`
- `simulated_orders`
- `simulated_positions`

SQLite files are runtime artifacts and are ignored by git.

## Safety Boundaries

The app does not implement real orders, real trading, real betting, wallet connection, deposits, withdrawals, cashout, external point exchange, private credentials, user-channel WebSocket authentication, or automated trading.

## Failure Handling

Live fetch errors, non-200 responses, JSON parse failures, and normalization failures are captured in structured fetch results. The app falls back to bundled sample data when needed and exposes diagnostic status through `/api/debug/source-status`.

## Test Strategy

Tests run offline. They cover sample loading, normalization shapes, storage snapshots, route responses, safety boundary scans, active market filtering, demo participation guards, demo point accounting, and helper script sample mode.
