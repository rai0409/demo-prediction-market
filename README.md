# Demo Prediction Market Viewer / 予想マーケット・デモビューア

FastAPIベースのローカル検証プロダクトです。外部予測市場の公開参考データを取得し、表示可能な市場だけをダッシュボードに出し、ローカルのデモポイントで `予想する` / `デモ参加する` 体験をシミュレーションできます。

## 重要な安全メモ

このリポジトリは技術検証用のローカルシミュレーションです。

- Polymarketへ注文を送信しません。
- Polymarket公式・提携・公認サービスではありません。
- ウォレット接続を実装していません。
- 入金、出金、換金、外部ポイント交換をサポートしません。
- 秘密鍵、シードフレーズ、APIキー、APIシークレットなどの private credentials を使いません。
- 投資助言、賭博助言、法的助言ではありません。

アプリ内の `デモポイント` は商用product基準の検証用非換金スコアです。購入、換金、出金、譲渡はできず、商品、ギフト券、Pay、株引換券、暗号資産、外部ポイント、景品とは交換できません。

このアプリはPolymarket公式・提携・公認サービスではありません。

## このプロジェクトで示していること

- FastAPI web app
- external public prediction-market reference data integration
- defensive data normalization
- sample data support
- SQLite storage
- market snapshot persistence
- active market filtering
- server-side eligibility guard
- local demo point history
- local demo point adjustment for internal checks
- optional public market freshness observations
- dashboard/detail/positions UI
- diagnostics endpoints
- automated tests

## 現在の確認済みステータス

- sample data mode works
- live Polymarket fetch works
- latest live check: 100 raw events fetched
- latest live check: 50 normalized markets
- latest live check: 21 displayable markets after filtering
- tests pass

Live API results depend on the public Gamma API response at runtime, so counts may change.

## アーキテクチャ概要

- `app/main.py`: FastAPI routes, Jinja rendering, API endpoints, formatting helpers.
- `app/polymarket_gamma.py`: public Gamma API fetch, response diagnostics, sample loader, defensive normalizer.
- `app/market_display.py`: active/current market classification, filtering, hidden-count metadata, demo eligibility labels.
- `app/demo_points.py`: local demo prediction validation, balance subtraction, simulated order/position creation.
- `app/storage.py`: SQLite connection, table creation, market snapshots, demo users, demo point history, simulated orders/positions.
- `app/realtime.py`: refresh orchestration, live-first/sample behavior, debug source status.
- `app/templates/` and `app/static/`: server-rendered dashboard, market detail, demo positions UI, refresh JS, CSS.
- `scripts/`: local live/sample fetch checks and raw Gamma response diagnostics.
- `tests/`: offline pytest coverage for normalization, storage, routes, safety boundary, filtering, and demo points.

## データフロー

1. Gamma API fetch or sample data load.
2. Normalize events/markets into one internal market shape.
3. Store current markets and snapshots in SQLite.
4. Classify markets for display and demo participation eligibility.
5. Render dashboard cards and detail pages from filtered markets.
6. Server-side guard checks eligibility on `POST /api/demo/predict`.
7. Store local simulated orders, simulated positions, and demo point history entries.
8. Expose diagnostics through runtime files and `/api/debug/source-status`.
9. Optionally collect public market freshness observations and display them separately from the main reference data.
10. Track demo point balance changes for internal verification.

## Setup

```bash
python -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
```

## Port policy

`demo-prediction-market` uses the 8090 port range.

- Default port: `8093`
- Alternative ports: `8094`, `8095`, `8096`
- Do not use the 8080 range for this project. Those ports are reserved for other projects such as Expo/mobile work.

## Run sample mode

```bash
DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8093
```

or:

```bash
scripts/run_sample.sh
```

## Run live mode

```bash
DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8093
```

or:

```bash
scripts/run_live.sh
```

Open `http://127.0.0.1:8093`.

## Limited check flow

1. Open `http://127.0.0.1:8093`.
2. Enter a limited participant code in `参加者` and switch users when testing multiple participants.
3. Confirm the dashboard shows only displayable markets.
4. Open a market detail page and confirm the non-cash demo point notice.
5. Use `デモ参加する` with demo points only.
6. Open `マイスコア`, `デモポジション`, and `結果確認`.
7. Confirm each participant has separate demo point balance, positions, results, and history.
8. Confirm the UI says demo points cannot be cashed out, transferred, or exchanged for products, gift cards, Pay balances, stock vouchers, crypto assets, external points, or prizes.

## Limited participant separation

The current limited operation uses a simple participant-code cookie. This keeps demo point balances, demo participation, positions, results, and history separated by participant.

This is not production authentication. It is not OAuth, email login, password login, KYC, payment, gift card exchange, Pay exchange, or exchangeable point infrastructure.

Internal operations such as demo point adjustment, balance reset, forced result recording, and manual data refresh are protected by `DEMO_ADMIN_TOKEN`. If `DEMO_ADMIN_TOKEN` is not set, those internal operations are disabled. State-changing requests also use a lightweight cookie token check and a small in-process rate limit.

## Result Confirmation Policy

Result confirmation is conservative. A result is reflected to the local Forecast Score only when the app can identify a clear outcome from stored reference data. A public result candidate alone is treated as a candidate, not a final result.

If a candidate and stored reference data disagree, the result remains on hold and no Forecast Score is added. Result rows keep the participant, target position, selected outcome, status, reflected score, reason, reference label, and confirmation time so the operation can be reviewed later.

Demo points remain non-cash, non-transferable, and non-exchangeable. They cannot be exchanged for products, gift cards, Pay balances, stock vouchers, crypto assets, external points, or prizes.

## Developer diagnostics

The following commands are for internal checks only and are not part of the general user flow.

```bash
curl -s -X POST http://127.0.0.1:8093/api/refresh | python -m json.tool
curl -s http://127.0.0.1:8093/api/markets | python -m json.tool
curl -s 'http://127.0.0.1:8093/api/markets?include_all=true' | python -m json.tool
curl -s http://127.0.0.1:8093/api/debug/source-status | python -m json.tool
```

Optional public market WebSocket freshness mode is off by default and is not required for the app.

```bash
DEMO_PREDICTION_LIVE=1 DEMO_PREDICTION_WS_ENABLED=1 python scripts/run_market_ws.py
```

Settings:

```bash
DEMO_PREDICTION_WS_ENABLED=0
DEMO_PREDICTION_WS_TOP_N=10
DEMO_PREDICTION_WS_STALE_SECONDS=90
```

The FastAPI app does not start the socket automatically. The standalone runner stores public market observations for developer diagnostics. The general UI uses user-facing update labels instead of transport-level wording.

See [docs/public_market_websocket.md](docs/public_market_websocket.md).

## Tests

```bash
python -m pytest tests -q
```

## Demo flow

1. Open the dashboard.
2. Refresh live data with `POST /api/refresh` or run with `DEMO_PREDICTION_LIVE=1`.
3. Open `マイスコア` to review the non-cash demo point balance and `デモポイント履歴`.
4. Open a market detail page.
5. Choose an outcome.
6. Enter demo points.
7. Click `デモ参加する`.
8. View `デモポジション`.

## API endpoints

HTML:

- `GET /`: dashboard with filtered markets.
- `GET /markets/{market_id}`: market detail and demo participation panel when eligible.
- `GET /demo-positions`: local simulated positions, `予想履歴`, and demo point history.
- `GET /health`: app health.

JSON:

- `POST /api/refresh`: fetches live/sample markets and stores snapshots.
- `GET /api/markets`: filtered displayable markets and hidden-count metadata.
- `GET /api/markets?include_all=true`: internal check for all stored normalized markets.
- `GET /api/markets/{market_id}`: one enriched market.
- `GET /api/markets/{market_id}/snapshots`: recent stored snapshots.
- `GET /api/debug/source-status`: live/sample/filter diagnostics.
- `GET /api/realtime/status`: optional public WebSocket freshness diagnostics.
- `GET /api/demo/balance`: local demo balance.
- `GET /api/demo/wallet`: local demo point history/internal record summary.
- `POST /api/demo/wallet/add-points`: local `デモポイント追加`.
- `POST /api/demo/wallet/reset`: local `デモ残高リセット`.
- `GET /api/demo/resolution-candidates`: public WebSocket `market_resolved` observations treated as `結果候補`.
- `GET /api/demo/positions`: local positions/orders/demo point history.
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

## Phase 3 TODO

- Authentication and role-based access for internal operations.
- CSRF protection for state-changing forms.
- Rate limits for public and demo-participation endpoints.
- Production-grade administrator permissions and audit review screens.
- Abuse/fraud detection and operational monitoring.

## v0.7 Freshness And Results Foundation

Optional REST auto-refresh can be enabled with:

```bash
DEMO_PREDICTION_AUTO_REFRESH=1
DEMO_PREDICTION_REFRESH_SECONDS=30
```

When enabled, dashboard and market API reads refresh only when stored data is stale. The interval is clamped between 15 and 300 seconds.

Local demo participation now creates a result-check row. `/demo-results` and `/api/demo/results` show result tracking fields such as `結果待ち`, `参加デモポイント`, `参加時確率`, `参考スコア`, `確定理由`, `参照元`, and `結果確定日時`.

Automatic result reflection is intentionally conservative. See [docs/demo_results_and_settlement.md](docs/demo_results_and_settlement.md).

## v0.9 Public Market WebSocket Freshness

v0.9 adds an optional public market WebSocket observation layer. It captures market data events such as book updates, price changes, last trade price, best bid/ask, and `market_resolved` into local SQLite.

REST remains the canonical fetch path. WebSocket observations are displayed separately and do not overwrite REST probabilities. `market_resolved` observations are recorded but not used alone for demo settlement.

## v1.0 Demo Point Ledger Foundation

v1.0 adds internal demo point adjustment, richer history metadata, idempotency keys for local demo actions, and internal records.

New ledger rows include `balance_before`, `balance_after`, `reference_type`, `reference_id`, `idempotency_key`, and `request_id`. Demo participation, demo point addition, demo balance reset, and demo settlement all remain local-only simulation records.

See [docs/demo_wallet_ledger.md](docs/demo_wallet_ledger.md).

## v1.1 REST-Confirmed Resolution Candidates

v1.1 uses public `market_resolved` observations as `結果候補`. A candidate can show that a result candidate exists, but it never reflects Forecast Score by itself.

Local result reflection requires conservative confirmation from stored reference data. If a candidate and reference data agree, the result can be recorded. If reference data is clear without a candidate, the app can also record the result. If only a candidate exists, or if candidate and reference data disagree, the result stays on hold and no Forecast Score is added.

## Roadmap

- screenshots
- optional charts
- better search/filter UI
- deploy as read-only demo
- CI
- Dockerfile
- richer tests
