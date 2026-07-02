# Demo Results And Settlement Foundation

v0.7 adds pending result tracking for local-only demo participation.

## Current Scope

When `POST /api/demo/predict` creates a local simulated position, the app also creates a `demo_settlements` row with status `pending`.

The result screen at `/demo-results` shows:

- 結果待ち
- 結果確定済み
- 参加デモポイント
- 参加時確率
- 推定デモリターン
- 精算デモポイント
- 判定ソース
- 結果確定日時

## Not Implemented Yet

Full automatic settlement is intentionally not implemented in v0.7.

The next settlement step should only mark results as `settled_win` or `settled_loss` when a clear and reliable winning outcome is available, for example:

- `winning_outcome` is explicitly available in normalized/stored market data, or
- market resolution state is confirmed through a safe public REST source, and
- the app can map the resolved outcome to the local simulated position outcome without ambiguity.

If resolution cannot be determined safely, the status should remain `pending`, `settlement_pending`, or `settlement_unknown`.

## Safety Boundary

Demo points have no monetary value. Result tracking and any future settlement are local simulation only.

The app does not place real orders, does not perform real trading, does not connect wallets, does not support deposits or withdrawals, does not support cashout, and does not exchange external points.
