# Demo Prediction Market Viewer / 予想マーケット・デモビューア

FastAPIベースのローカル技術MVPです。Polymarketの公開マーケットデータを取得し、アクティブで表示可能な市場だけをダッシュボードに出し、ローカルのデモポイントで `予想する` / `デモ参加する` 体験をシミュレーションできます。

## 重要な安全メモ

このリポジトリは技術検証用のローカルシミュレーションです。

- Polymarketへ注文を送信しません。
- ウォレット接続を実装していません。
- 入金、出金、換金、外部ポイント交換をサポートしません。
- 秘密鍵、シードフレーズ、APIキー、APIシークレットなどの private credentials を使いません。
- 投資助言、賭博助言、法的助言ではありません。

アプリ内の `デモポイント` は無償のシミュレーション専用ポイントであり、購入、換金、出金、譲渡、外部ポイント交換、暗号資産交換、景品交換はできません。

## このプロジェクトで示していること

- FastAPI web app
- Polymarket public Gamma API integration
- defensive API normalization
- sample fallback design
- SQLite storage
- market snapshot persistence
- active market filtering
- server-side eligibility guard
- local demo point ledger
- dashboard/detail/positions UI
- diagnostics endpoints
- automated tests

## 現在の確認済みステータス

- sample fallback works
- live Polymarket fetch works
- latest live check: 100 raw events fetched
- latest live check: 50 normalized markets
- latest live check: 21 displayable markets after filtering
- `/api/markets?include_all=true` returns all fetched normalized markets
- tests pass: `44 passed`

Live API results depend on the public Gamma API response at runtime, so counts may change.

## アーキテクチャ概要

- `app/main.py`: FastAPI routes, Jinja rendering, API endpoints, formatting helpers.
- `app/polymarket_gamma.py`: public Gamma API fetch, response diagnostics, sample fallback loader, defensive normalizer.
- `app/market_display.py`: active/current market classification, filtering, hidden-count metadata, demo eligibility labels.
- `app/demo_points.py`: local demo prediction validation, balance subtraction, simulated order/position creation.
- `app/storage.py`: SQLite connection, table creation, market snapshots, demo users, ledger, simulated orders/positions.
- `app/realtime.py`: refresh orchestration, live-first/sample-fallback behavior, debug source status.
- `app/templates/` and `app/static/`: server-rendered dashboard, market detail, demo positions UI, polling JS, CSS.
- `scripts/`: local live/sample fetch checks and raw Gamma response diagnostics.
- `tests/`: offline pytest coverage for normalization, storage, routes, safety boundary, filtering, and demo points.

## データフロー

1. Gamma API fetch or sample fallback load.
2. Normalize events/markets into one internal market shape.
3. Store current markets and snapshots in SQLite.
4. Classify markets for display and demo participation eligibility.
5. Render dashboard cards and detail pages from filtered markets.
6. Server-side guard checks eligibility on `POST /api/demo/predict`.
7. Store local simulated orders, simulated positions, and demo point ledger entries.
8. Expose diagnostics through runtime files and `/api/debug/source-status`.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Run sample mode

```bash
DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

or:

```bash
scripts/run_sample.sh
```

## Run live mode

```bash
DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092
```

or:

```bash
scripts/run_live.sh
```

Open `http://127.0.0.1:8092`.

## Refresh

```bash
curl -s -X POST http://127.0.0.1:8092/api/refresh | python -m json.tool
```

## Check filtered markets

```bash
curl -s http://127.0.0.1:8092/api/markets | python -m json.tool
```

## Check all markets

```bash
curl -s 'http://127.0.0.1:8092/api/markets?include_all=true' | python -m json.tool
```

## Debug

```bash
curl -s http://127.0.0.1:8092/api/debug/source-status | python -m json.tool
```

## Tests

```bash
python -m pytest tests -q
```

## Demo flow

1. Open the dashboard.
2. Refresh live data with `POST /api/refresh` or run with `DEMO_PREDICTION_LIVE=1`.
3. Open a market detail page.
4. Choose an outcome.
5. Enter demo points.
6. Click `デモ参加する`.
7. View `デモポジション`.

## API endpoints

HTML:

- `GET /`: dashboard with filtered markets.
- `GET /markets/{market_id}`: market detail and demo participation panel when eligible.
- `GET /demo-positions`: local simulated positions, `予想履歴`, and ledger.
- `GET /health`: app health.

JSON:

- `POST /api/refresh`: fetches live/sample markets and stores snapshots.
- `GET /api/markets`: filtered displayable markets and hidden-count metadata.
- `GET /api/markets?include_all=true`: all stored normalized markets.
- `GET /api/markets/{market_id}`: one enriched market.
- `GET /api/markets/{market_id}/snapshots`: recent stored snapshots.
- `GET /api/debug/source-status`: live/sample/fallback/filter diagnostics.
- `GET /api/demo/balance`: local demo balance.
- `GET /api/demo/positions`: local positions/orders/ledger.
- `POST /api/demo/predict`: local-only demo participation. It never calls external trading APIs.

See [docs/api.md](docs/api.md) for details.

## Screenshots

Recommended screenshot paths:

- `docs/screenshots/dashboard-live.png`
- `docs/screenshots/market-detail.png`
- `docs/screenshots/demo-positions.png`
- `docs/screenshots/debug-source-status.png`

Actual screenshots are not committed yet.

## Limitations

- Local demo only.
- No authentication.
- No production deployment setup.
- No real order placement.
- Public API format may change.
- Legal/regulatory review is required before any real-money, external point, or production use.
- Not production-ready.

## v0.7 Freshness And Results Foundation

Optional REST auto-refresh can be enabled with:

```bash
DEMO_PREDICTION_AUTO_REFRESH=1
DEMO_PREDICTION_REFRESH_SECONDS=30
```

When enabled, dashboard and market API reads refresh only when stored data is stale. The interval is clamped between 15 and 300 seconds.

Local demo participation now creates a pending result row. `/demo-results` and `/api/demo/results` show result tracking fields such as `結果待ち`, `参加デモポイント`, `参加時確率`, `推定デモリターン`, `精算デモポイント`, and `判定ソース`.

Full automatic settlement is intentionally not implemented yet. See [docs/demo_results_and_settlement.md](docs/demo_results_and_settlement.md).

## Roadmap

- screenshots
- optional charts
- better search/filter UI
- deploy as read-only demo
- CI
- Dockerfile
- richer tests
