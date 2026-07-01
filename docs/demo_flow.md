# Demo Flow

## Sample Mode Walkthrough

1. Install dependencies.
2. Run `DEMO_PREDICTION_LIVE=0 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092`.
3. Open `http://127.0.0.1:8092`.
4. The dashboard uses `data/sample_events.json`.
5. Open a market detail page and use `デモ参加する` to create a local simulated position.

## Live Mode Walkthrough

1. Run `DEMO_PREDICTION_LIVE=1 python -m uvicorn app.main:app --host 127.0.0.1 --port 8092`.
2. The app attempts a public Polymarket Gamma API fetch.
3. If successful, source is `polymarket` and `data_source_status` is `live`.
4. If fetch fails, bundled sample data is used and the fallback status is shown.

## Dashboard Walkthrough

The dashboard shows:

- `デモ残高`
- data source badge
- displayed market count
- total fetched market count
- hidden market counts
- last fetch time
- filtered market cards

The default dashboard filters out closed, inactive, expired, zero-liquidity, and resolved-looking markets.

## Market Detail Walkthrough

The market detail page shows:

- title/question
- source/status
- display status
- end date
- volume, 24h volume, liquidity
- outcome probabilities
- snapshot trend/table
- `予想する` panel only when eligible

## Demo Participation Walkthrough

1. Choose an outcome.
2. Enter demo points.
3. Review estimated simulation return and maximum demo point loss.
4. Click `デモ参加する`.
5. The server validates market eligibility, outcome, stake, and demo balance.
6. The app stores only local simulated records.

## Demo Positions Walkthrough

Open `/demo-positions` to review:

- current demo balance
- simulated positions
- `予想履歴`
- demo point ledger

## Debug Endpoint Walkthrough

Run:

```bash
curl -s http://127.0.0.1:8092/api/debug/source-status | python -m json.tool
```

Expected successful live indicators:

- `last_fetch_status` or status from refresh is `live`
- market `source` is `polymarket`
- market `data_source_status` is `live`
- `fallback_used` is `false`
- `count` can be smaller than `total_market_count` because filtering is applied
