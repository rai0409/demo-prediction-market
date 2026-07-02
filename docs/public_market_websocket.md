# Public Market WebSocket Freshness

v0.9 adds an optional public market WebSocket freshness layer.

## Scope

This layer is for public market data only. It does not use authentication, private credentials, wallet data, or any private stream.

Endpoint:

```text
wss://ws-subscriptions-clob.polymarket.com/ws/market
```

Subscription shape:

```json
{
  "assets_ids": ["..."],
  "type": "market",
  "custom_feature_enabled": true
}
```

The `assets_ids` spelling matches public Polymarket market channel examples.

## Configuration

Default is disabled.

```bash
DEMO_PREDICTION_WS_ENABLED=0
DEMO_PREDICTION_WS_TOP_N=10
DEMO_PREDICTION_WS_STALE_SECONDS=90
```

- `DEMO_PREDICTION_WS_ENABLED`: enables the standalone WebSocket runner when set to `1`, `true`, `yes`, or `on`.
- `DEMO_PREDICTION_WS_TOP_N`: number of displayable high-volume/liquid markets to subscribe to, clamped from 1 to 50.
- `DEMO_PREDICTION_WS_STALE_SECONDS`: freshness window for stored WebSocket observations, clamped from 15 to 600 seconds.

## Runner

The FastAPI app does not start a socket automatically.

Manual runner:

```bash
DEMO_PREDICTION_LIVE=1 DEMO_PREDICTION_WS_ENABLED=1 python scripts/run_market_ws.py
```

The runner:

1. loads settings,
2. initializes SQLite,
3. ensures markets exist through the existing REST/sample path,
4. selects top markets with asset ids,
5. subscribes to the public market channel,
6. stores received observations in `market_realtime_updates`.

## Captured Fields

The parser stores public market observations for:

- `book`
- `price_change`
- `last_trade_price`
- `best_bid_ask`
- `market_resolved`

Stored fields include best bid, best ask, last trade price, price, size, side, spread, winning outcome, winning asset id, raw event JSON, event timestamp, and received timestamp.

## Display Behavior

REST market data remains the canonical dashboard source. WebSocket observations are displayed separately:

- `リアルタイム状態`
- `WebSocket更新中`
- `WebSocket stale`
- `RESTのみ`
- `最良買い気配`
- `最良売り気配`
- `直近取引価格`
- `WebSocket最終更新`

The app does not overwrite REST probabilities with WebSocket observations in v0.9.

## REST Fallback

If WebSocket mode is disabled, unavailable, stale, or no asset ids are present, the existing REST/sample behavior remains unchanged.

## market_resolved Events

`market_resolved` events are recorded as public realtime observations and treated as `結果候補`.

They are not trusted alone for local demo settlement. v1.1 uses them only to prioritize and explain result checking:

- `WS検知あり`: a public WebSocket candidate exists.
- `WSのみ未確認`: REST confirmation is not clear yet, so no local demo settlement occurs.
- `REST確認済み`: WebSocket candidate and REST/conservative result agree.
- `WS/REST不一致`: candidate and REST disagree, so settlement is blocked as `判定不明`.

REST-only conservative settlement still works when no WebSocket candidate exists.

## Safety Boundary

This feature does not place orders, does not connect wallets, does not use API keys, does not use private keys, and does not move money or external points.
