# Demo Prediction Market Viewer

日本語表示名: 予想マーケット・デモビューア

このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは購入・換金・出金・譲渡・外部ポイント交換・暗号資産交換・景品交換ができません。
このアプリはPolymarketへの注文、ウォレット接続、入金、出金、売買を行いません。

## Overview

Demo Prediction Market Viewer is a local FastAPI MVP. It shows public Polymarket-style market data when live mode is enabled, otherwise it uses bundled sample fallback data. Users can press `予想する` and `デモ参加する` to record local simulated positions with free `デモポイント`.

## Safety Boundary

Intentionally not implemented:

- real orders or real trading
- wallet connection
- deposit, withdrawal, or cashout functionality
- conversion to money, crypto, gifts, or external points
- paid point purchase
- private keys, seed phrases, API keys, API secrets, or passphrases
- authenticated user WebSocket channels
- automated trading

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run In Sample Mode

```bash
DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Open `http://127.0.0.1:8092`.

## Run With Optional Public Fetch

```bash
DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Live mode uses only public market data and falls back to `data/sample_events.json` if the fetch fails. No secrets are required.

## Tests

```bash
python -m pytest tests -q
python -c "from app.main import app; print(app.title)"
```

## Demo Points

The app creates one local demo user, `local-demo-user`, with `10000` free demo points. Demo participation subtracts points locally and records simulated positions, simulated order history, and a demo point ledger.

Demo points are simulation-only and have no monetary value.

## v0.2 Live Fetch And Diagnostics

Sample mode remains the default:

```bash
DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Live mode attempts the public Polymarket Gamma events endpoint first:

```bash
DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Accepted enabled values are `1`, `true`, `True`, `yes`, and `on`. Accepted disabled values are `0`, `false`, `False`, `no`, `off`, and an empty value.

Refresh current markets and store snapshots:

```bash
curl -X POST http://127.0.0.1:8092/api/refresh
```

Inspect source status:

```bash
curl http://127.0.0.1:8092/api/debug/source-status
```

Run the fallback/live helper:

```bash
DEMO_PREDICTION_LIVE=0 python scripts/check_live_fetch.py
DEMO_PREDICTION_LIVE=1 python scripts/check_live_fetch.py
```

Dump one raw public Gamma response for diagnosis:

```bash
python scripts/dump_gamma_sample.py
```

Runtime diagnostics are written under `runtime/` and are intentionally ignored by git:

- `runtime/gamma_last_response.json`
- `runtime/gamma_last_error.txt`
- `runtime/gamma_last_status.json`
- `runtime/manual_gamma_response.json`

Fetch statuses:

- `live`: public Gamma fetch succeeded and displayable Polymarket markets were normalized.
- `live_failed_sample_fallback`: live fetch or normalization failed, so bundled sample data was used.
- `live_empty_sample_fallback`: live fetch succeeded but produced no displayable markets, so bundled sample data was used.
- `sample_fallback`: sample mode is active or sample data was intentionally loaded.

The safety boundary is unchanged: no real orders, no wallet, no deposit, no withdrawal, no cashout, no external point exchange, and no private credentials.

## v0.3 Active Market Filtering

Live Polymarket fetch remains supported with `DEMO_PREDICTION_LIVE=1`. The default dashboard now filters the normal market list to current displayable markets.

Filtered out by default:

- closed markets
- inactive markets
- expired markets
- markets with no displayable outcomes or probabilities
- markets with no liquidity
- markets whose probabilities look resolved, such as 0% / 100%

The market API returns count metadata for displayed and hidden markets:

```bash
curl http://127.0.0.1:8092/api/markets
```

To inspect every fetched market, including archived or hidden rows:

```bash
curl "http://127.0.0.1:8092/api/markets?include_all=true"
```

Additional filters are available:

- `include_closed=true`
- `include_expired=true`
- `include_inactive=true`
- `include_all=true`

`POST /api/demo/predict` now enforces the same eligibility boundary server-side. Local demo participation is rejected for closed, inactive, expired, zero-liquidity, and resolved-looking markets even if a caller bypasses the UI.

The safety boundary remains unchanged: the app still does not place real orders, connect wallets, support deposits, support withdrawals, support cashout, exchange external points, or use private credentials.

## v0.4 UI Polish And Demo Flow

This remains a local technical MVP, not a production service and not legal, investment, betting, or trading advice.

Current status:

- live Polymarket public data fetch works when `DEMO_PREDICTION_LIVE=1`
- the dashboard filters active/current markets by default
- ineligible markets are blocked server-side for demo participation
- the UI includes status badges, probability bars, hidden-market summaries, detail metrics, snapshot history, and demo positions
- demo participation is local-only and uses free simulation-only demo points

Run sample mode:

```bash
DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Run live public data mode:

```bash
DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

Run tests:

```bash
python -m pytest tests -q
```

The safety boundary remains unchanged: no real orders, no wallet, no deposit, no withdrawal, no cashout, no external point exchange, and no private credentials.
