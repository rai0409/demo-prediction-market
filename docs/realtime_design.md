# Realtime Design

Demo Prediction Market Viewer uses REST polling first.

The default mode is sample fallback mode. `data/sample_events.json` is loaded, normalized, and stored in SQLite so the app works offline and tests never require network access.

When `DEMO_PREDICTION_LIVE=1`, the app attempts a public Gamma API-style fetch from Polymarket event data. If that public fetch fails, the app falls back to sample data and marks the data source status with an explicit fallback value such as `live_failed_sample_fallback`.

`POST /api/refresh` fetches current public/sample markets and stores market snapshots. `GET /api/markets` returns filtered displayable market cards by default. `GET /api/markets?include_all=true` returns all stored normalized markets. `GET /api/markets/{market_id}/snapshots` returns recent local snapshots.

The dashboard JavaScript polls `/api/markets` every 15 to 30 seconds and updates visible freshness and data source status.

`app/polymarket_market_ws.py` is a disabled-by-default skeleton only. It does not connect automatically, does not authenticate a user channel, does not use credentials, and does not place orders. WebSocket support is disabled because REST polling is enough for this local MVP and avoids accidental trading-style behavior.
