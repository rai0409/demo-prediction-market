Title: Build Realtime Demo Prediction Market MVP

Repository context:
This is a new standalone repository.
Do not assume codex-local-runner internal structure.
Do not modify files outside this repository.
Do not use prompt-number naming such as prompt691 in product files, app names, routes, or docs.

Goal:
Build a local MVP called Demo Prediction Market Viewer.
Japanese display name: 予想マーケット・デモビューア.

The app must show public Polymarket-style prediction market data when available and allow local-only demo participation using demo points.

This app is not a real trading app.
This app is not a real betting app.
This app is not a wallet app.
This app is not a deposit, withdrawal, cashout, crypto, gift, or external point exchange app.

Main product concept:
The product has two layers.

Layer 1: Public market viewer
- Show public prediction market data.
- Prefer Polymarket public Gamma API style data when live mode is enabled.
- Use local sample fallback when live mode is disabled or live fetch fails.
- Show market title, market question, outcomes, implied probabilities, volume, 24h volume, liquidity, end date, and freshness timestamp.

Layer 2: Demo participation
- The user can press 予想する.
- The user can choose YES or NO, or another normalized market outcome when available.
- The user can enter demo points.
- The user can press デモ参加する.
- The app records a local simulated position.
- The app subtracts demo points locally.
- The app records a demo point ledger entry.
- No external order is sent.
- No real Polymarket order is sent.
- No wallet is connected.
- No money, crypto, external points, gifts, prizes, or cashout exist.

Required technical stack:
- FastAPI
- SQLite
- Jinja2 templates
- simple JavaScript polling
- pytest
- no secrets
- no wallet
- no private credentials
- no real order placement

Required repository structure:
Create these files and directories.

app/__init__.py
app/main.py
app/config.py
app/storage.py
app/polymarket_gamma.py
app/demo_points.py
app/realtime.py
app/safety.py
app/polymarket_market_ws.py
app/templates/base.html
app/templates/index.html
app/templates/market_detail.html
app/templates/demo_positions.html
app/static/app.js
app/static/styles.css
data/sample_events.json
docs/realtime_design.md
docs/demo_participation_boundary.md
docs/legal_safety_notes.md
tests/test_gamma_normalizer.py
tests/test_storage.py
tests/test_demo_points.py
tests/test_routes.py
tests/test_safety_boundary.py
requirements.txt
README.md

User-facing wording policy:
Use these words in the UI:
- 予想する
- デモ参加
- デモ参加する
- デモポイント
- 予想履歴
- デモポジション
- デモ残高
- 参加内容を確認
- この内容でデモ参加

Do not use these words as UI action labels:
- 賭ける
- ベット
- Bet now
- place bet
- buy
- sell
- 入金
- 出金
- 換金
- 稼ぐ
- 儲かる
- 利益確定

It is acceptable to mention prohibited concepts in README and docs as safety disclaimers, but not as implemented actions, buttons, navigation labels, or API actions.

Required disclaimer:
Show the following exact disclaimer prominently on dashboard, market detail page, demo participation panel, demo positions page, and README.

このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは購入・換金・出金・譲渡・外部ポイント交換・暗号資産交換・景品交換ができません。
このアプリはPolymarketへの注文、ウォレット接続、入金、出金、売買を行いません。

Hard forbidden implementation:
Do not implement any of the following:
- real betting
- real trading
- real order placement
- Polymarket CLOB trading client
- wallet connection
- private key
- seed phrase
- API key
- API secret
- passphrase
- deposit functionality
- withdrawal functionality
- cashout functionality
- conversion to money
- conversion to crypto
- conversion to gift cards
- conversion to external points
- paid point purchase
- affiliate registration
- user channel WebSocket authentication
- automated trading
- transaction signing
- private user balance sync from external services

Do not create these routes:
- /buy
- /sell
- /bet
- /deposit
- /withdraw
- /cashout
- /wallet
- /order/place
- /api/demo/bet
- /api/order
- /api/trade
- /api/wallet

Allowed routes:
- GET /
- GET /markets/{market_id}
- GET /demo-positions
- GET /health
- GET /api/markets
- GET /api/markets/{market_id}
- GET /api/markets/{market_id}/snapshots
- POST /api/refresh
- GET /api/demo/balance
- GET /api/demo/positions
- POST /api/demo/predict

Use POST /api/demo/predict for local demo participation.
Do not create /api/demo/bet.

Configuration:
Implement app/config.py with these environment variables.

DEMO_PREDICTION_LIVE:
- default: 0
- allowed: 0 or 1
- when 0, use sample data only
- when 1, try live public fetch and fall back to sample data on failure

DEMO_PREDICTION_POLL_SECONDS:
- default: 30

DEMO_PREDICTION_LIMIT:
- default: 50

DEMO_PREDICTION_DB:
- default: data/demo_prediction.sqlite3

Data source:
Implement public Polymarket market discovery using this Gamma API style endpoint when live mode is enabled:

https://gamma-api.polymarket.com/events?active=true&closed=false&order=volume_24hr&ascending=false&limit=100

Do not require secrets.
Do not require API keys.
Do not require wallet connection.

Sample fallback:
Create data/sample_events.json.
It must be small, sanitized, and sufficient for offline tests and demo UI.

The sample data must include at least 3 markets.
Each sample market should include:
- event id
- market id
- slug
- title/question
- outcomes YES and NO
- outcome prices or probabilities
- volume
- volume_24hr
- liquidity
- active
- closed
- end_date
- description or resolution condition

Normalization:
Implement app/polymarket_gamma.py to normalize both live-style Gamma API event data and local sample data into one internal structure.

Internal market fields:
- market_id
- source
- external_event_id
- external_market_id
- slug
- title
- question
- description
- outcomes
- probabilities
- volume
- volume_24hr
- liquidity
- active
- closed
- end_date
- fetched_at
- data_source_status

If exact Gamma field names differ, implement defensive parsing and keep tests based on sample data.

Realtime behavior:
Implement near-real-time behavior with REST polling first.

Required behavior:
- POST /api/refresh fetches public events or sample data and stores snapshots.
- GET /api/markets returns normalized market cards.
- GET /api/markets/{market_id}/snapshots returns recent local snapshots.
- Dashboard JavaScript polls /api/markets every 15 to 30 seconds.
- Show data freshness timestamp in the UI.
- Show data source status in the UI: live, sample fallback, or last fetch failed.

WebSocket skeleton:
Create app/polymarket_market_ws.py.
It must be disabled by default.
It should document future public market WebSocket support only.
It must not connect automatically.
It must not implement user channel.
It must not use credentials.
It must not place orders.
It must not expose routes that start live WebSocket trading behavior.

Demo point model:
Create one local demo user automatically.

demo_user_id:
local-demo-user

initial_demo_points:
10000

Demo point rules:
- Demo points are created locally.
- Demo points are free.
- Demo points cannot be purchased.
- Demo points cannot be withdrawn.
- Demo points cannot be converted to money.
- Demo points cannot be converted to crypto.
- Demo points cannot be exchanged for external points.
- Demo points cannot be exchanged for gifts or prizes.
- Demo points cannot be transferred.
- Demo points are only for local simulation.

Demo participation behavior:
On the market detail page:
- Show a panel titled 予想する.
- Show selectable outcomes.
- Show an input for demo points.
- Show current implied probability.
- Show estimated simulation return.
- Show maximum demo point loss.
- Show no cashout disclaimer.
- Show a button labeled デモ参加する.

When POST /api/demo/predict is called:
- validate the market exists
- validate the outcome exists
- validate stake is numeric
- validate stake is greater than 0
- validate the demo user has sufficient demo points
- subtract stake from demo balance
- create simulated order record
- create simulated position record
- create demo point ledger entry
- return updated balance and created position
- do not call external APIs
- do not place orders
- do not connect wallet

Estimated return:
Use a simple simulation formula based on implied probability.
For example, if probability is 0.60, estimated return for 100 demo points may be about 166.67 demo points.
Use safe display language: estimated simulation return.
Do not display profit guarantees.
Do not say the user will earn money.

SQLite storage:
Use SQLite.
Create tables if they do not exist.

Required tables:
- markets
- market_snapshots
- fetch_runs
- demo_users
- demo_point_ledger
- simulated_orders
- simulated_positions

Do not commit SQLite DB files.

Dashboard UI:
GET / must show:
- required disclaimer
- product name
- data source status
- freshness timestamp
- demo point balance
- top active markets by 24h volume
- market title
- probabilities
- volume
- 24h volume
- liquidity
- end date
- link or button labeled 予想する

Market detail UI:
GET /markets/{market_id} must show:
- required disclaimer
- market title/question
- outcomes and probabilities
- volume
- 24h volume
- liquidity
- end date
- description or resolution condition if available
- recent snapshot history
- simple probability trend visualization using HTML/SVG/plain JS
- 予想する panel
- デモ参加する button
- no monetary value disclaimer

Demo positions UI:
GET /demo-positions must show:
- required disclaimer
- current demo balance
- simulated positions
- simulated order history
- demo point ledger
- no monetary value disclaimer

Static JavaScript:
app/static/app.js should:
- poll /api/markets every 15 to 30 seconds
- update visible freshness/data status if possible
- keep behavior simple and resilient
- not use external dependencies

Static CSS:
app/static/styles.css should:
- provide clean dashboard layout
- make disclaimers visible
- make market cards readable
- make demo participation panel clear
- avoid dark patterns or trading-style urgency

Tests:
All tests must run offline.
Do not require network.

Add tests for:
- sample fallback loading
- market normalization
- snapshot storage
- demo user initial balance
- successful demo prediction subtracts demo points
- insufficient demo points rejected
- invalid outcome rejected
- no forbidden routes exist
- no wallet/deposit/withdraw/cashout implementation exists
- UI text uses 予想する
- UI text uses デモ参加
- app UI action text does not use 賭ける
- app UI action text does not use ベット
- GET /health
- GET /api/markets
- POST /api/demo/predict

Safety tests:
Create tests/test_safety_boundary.py.
It should inspect app routes and source text under app/.
It should fail if forbidden route paths exist.
It should fail if implementation code contains dangerous function names such as:
- place_order
- create_order
- submit_order
- wallet_connect
- connect_wallet
- private_key
- seed phrase
- mnemonic
- CLOBClient
- py_clob_client

It is acceptable for docs and README to mention forbidden concepts as safety disclaimers.
Do not make tests fail only because README/docs say "does not support deposit" or "no cashout".

Documentation:
Create docs/realtime_design.md.
It must explain REST polling first, sample fallback, live public fetch mode, future WebSocket skeleton, and why WebSocket is disabled by default.

Create docs/demo_participation_boundary.md.
It must explain demo points, no purchase, no withdrawal, no cashout, no transfer, no external exchange, and local-only simulated positions.

Create docs/legal_safety_notes.md.
It must explain this is a technical MVP, not legal advice, not investment advice, not betting advice, no real trading, no wallet, and no monetary value.

README:
Update README.md with:
- app overview
- safety boundary
- setup
- install dependencies
- sample mode run
- live public fetch run
- test commands
- demo point explanation
- intentionally not implemented features
- local run command

Requirements:
Create requirements.txt with only necessary packages.
Use FastAPI, uvicorn, jinja2, requests or httpx, pytest.
Prefer minimal dependencies.

Verification:
Run:
python -m pytest tests -q

Also run:
python -c "from app.main import app; print(app.title)"

Do not push to GitHub.
Do not create GitHub repo.
Do not commit database files.
Do not commit runtime files.
Do not commit secrets.

Commit behavior:
If implementation and tests pass, create a local git commit with this message:
Build realtime demo prediction market MVP

If tests do not pass, do not commit. Report failures and what remains.

Final report:
Report:
- whether implementation completed
- tests run
- pass/fail result
- commit hash if committed
- whether live fetch was tested or sample fallback only
- main app path
- local run command
- safety confirmation:
  - uses 予想する / デモ参加
  - no real orders
  - no wallet
  - no deposit
  - no withdrawal
  - no cashout
  - no external point exchange
  - no private credentials
