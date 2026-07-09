# Demo Participation Boundary

This app is a commercial-product-standard local simulation only.

## Local Demo User

The app creates one local demo user:

- `demo_user_id`: `local-demo-user`
- initial demo points: `10000`

## Demo Point Rules

Demo points are free simulation-only points.

Demo points:

- have no monetary value
- cannot be purchased
- cannot be withdrawn
- cannot be cashed out
- cannot be transferred
- cannot be converted to money
- cannot be converted to crypto
- cannot be exchanged for external points
- cannot be exchanged for gifts or prizes

## Demo Participation Flow

When a user presses `デモ参加する`, the app validates:

- market exists
- market is active/displayable and eligible for demo participation
- outcome exists
- stake is numeric
- stake is greater than zero
- local demo balance is sufficient

If valid, the app subtracts demo points locally, records a simulated order, records a simulated position, and writes a local ledger entry.

## Hard Boundary

No Polymarket order is placed. No external trading API is called. No wallet is connected. No private credentials are used. No real betting or trading is implemented.
