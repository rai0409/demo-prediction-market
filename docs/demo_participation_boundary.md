# Demo Participation Boundary

Demo participation is local simulation only.

The app creates one local demo user:

- `demo_user_id`: `local-demo-user`
- initial demo points: `10000`

Demo points are free local simulation points. They cannot be purchased, withdrawn, cashed out, transferred, converted to money, converted to crypto, exchanged for external points, or exchanged for gifts or prizes.

When a user presses `デモ参加する`, the app validates the market, outcome, stake, and available demo balance. It subtracts demo points locally, records a simulated order, records a simulated position, and writes a local ledger entry.

No external trading API is called. No wallet is connected. No private credentials are used.
