# Demo Results And Settlement

v0.7 added pending result tracking for local-only demo participation. v0.8 adds conservative local-only demo settlement.

## Current Scope

When `POST /api/demo/predict` creates a local simulated position, the app also creates a `demo_settlements` row with status `pending`.

The result screen at `/demo-results` shows result rows and a `結果を確認する` action. That action calls `POST /api/demo/settle`, checks pending rows against stored public market data, and updates only local SQLite demo records.

- 結果待ち
- 結果確定済み
- 参加デモポイント
- 参加時確率
- 推定デモリターン
- 精算デモポイント
- 判定ソース
- 結果確定日時

## Conservative Settlement Rules

The app settles only when a winning outcome is clear.

Preferred explicit fields include:

- `winning_outcome`
- `winningOutcome`
- `resolved_outcome`
- `resolution_outcome`
- `winningOutcomeName`
- `winning_asset_id`
- `winningAssetId`

For winning asset ids, the id is mapped to an outcome only when stored market data has an unambiguous token/outcome mapping.

The fallback probability rule is intentionally strict. The app may infer a winning outcome only when:

- the market is closed or resolved,
- outcomes are available,
- probabilities are available,
- exactly one outcome has probability `>= 0.999`, and
- every other outcome has probability `<= 0.001`.

The app does not infer a result from `closed=true` alone, `active=false` alone, an end date alone, or high-but-not-final probabilities such as `0.8` or `0.9`.

If resolution cannot be determined safely, the status remains `pending`, `settlement_pending`, or `settlement_unknown`.

## Idempotency

Settlement is designed to avoid double payout:

- rows already marked `settled_win` or `settled_loss` are returned unchanged,
- ledger notes include `settlement_id=<id>`,
- ledger rows use `reference_type=demo_settlement` and `reference_id=<id>`,
- repeated settlement calls do not add a second win payout,
- loss settlement records a zero-amount ledger entry once.

v1.0 settlement ledger rows also include `balance_before` and `balance_after`, and settlement checks create local audit events.

## Statuses

- `pending`: result has not been checked yet.
- `settlement_pending`: public data does not show a clear result yet.
- `settlement_unknown`: required local market data is missing.
- `settled_win`: selected outcome matched the clear winning outcome.
- `settled_loss`: selected outcome did not match the clear winning outcome.

## Safety Boundary

Demo points have no monetary value. Result tracking and any future settlement are local simulation only.

The app does not place real orders, does not perform real trading, does not connect wallets, does not support deposits or withdrawals, does not support cashout, and does not exchange external points.
