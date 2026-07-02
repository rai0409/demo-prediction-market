# Demo Point Ledger

v1.0 adds a more commercial-grade local demo point ledger foundation.

## Boundary

Demo points remain demo points forever.

- no monetary value
- no external exchange
- no cashout
- no deposit or withdrawal feature
- no crypto wallet
- no private keys
- no API secrets
- no real orders
- no Polymarket trading endpoint

The app records local simulation state only.

## Ledger Structure

New `demo_point_ledger` rows include:

- `balance_before`
- `balance_after`
- `reference_type`
- `reference_id`
- `idempotency_key`
- `request_id`

Older rows may have null metadata fields because the migration is backward-compatible.

Entry types include:

- `initial`
- `prediction`
- `demo_point_add`
- `demo_balance_reset`
- `settlement_win`
- `settlement_loss`

## Idempotency

Demo actions can include an `idempotency_key`.

When the same key is repeated for the same local user/action:

- demo point addition returns the previous ledger result,
- demo balance reset returns the previous ledger result,
- demo participation returns the previous simulated position result,
- no duplicate balance movement is created.

Calls without an idempotency key keep the previous behavior and create a new local action each time.

## Audit Events

`demo_audit_events` records local processing events with:

- `event_type`
- `user_id`
- `route`
- `request_id`
- `reference_type`
- `reference_id`
- `before_json`
- `after_json`
- `note`
- `created_at`

Audit events are local SQLite records for traceability, not compliance evidence.

## Balance Reconciliation

For new ledger rows, balance reconciliation can be checked by comparing:

```text
balance_before + amount = balance_after
```

Settlement win rows add local demo points. Settlement loss rows record a zero-amount ledger row. Both use `reference_type=demo_settlement`.

## Local-Only Limitations

This is not a production point system. It has no authentication, no multi-user security model, no external reconciliation, and no legal/regulatory review.
