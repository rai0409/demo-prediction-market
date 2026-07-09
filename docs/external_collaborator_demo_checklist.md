# External Collaborator Demo Checklist

Use this checklist before showing the limited demo to external collaborators. This is an operations checklist only; it does not add authentication, payment, exchangeable points, correction workflows, or destructive admin actions.

## Environment

- Confirm the app is served on the 8090 port range: default `8093`, alternatives `8094`, `8095`, `8096`.
- Set `DEMO_ADMIN_TOKEN` to a strong random management code before enabling internal operations.
- Set `DEMO_COOKIE_SECURE=1` when serving over HTTPS. Keep it `0` for plain local HTTP.
- Set `DEMO_PREDICTION_MAX_DEMO_STAKE` to the intended per-participation cap.
- Confirm `DEMO_PREDICTION_LIVE` matches the planned data mode.
- Confirm demo points remain non-cash, non-transferable, and non-exchangeable.

## Participant Setup

- Prepare participant codes before the session.
- Share each participant code only with the intended collaborator.
- Explain that participant codes separate demo balances, positions, results, and history, but are not production authentication.
- Confirm participants understand that demo points cannot be exchanged for products, gift cards, Pay balances, stock vouchers, crypto assets, external points, or prizes.
- Confirm the app is not official, endorsed by, or affiliated with Polymarket.

## Admin Handling

- Do not share `/admin/audit` or `/admin/audit.csv` links with general participants.
- Do not put the management code in a URL.
- Use the management-code form or the `x-demo-admin-token` header for internal checks.
- Confirm `/admin/audit` rejects unauthenticated access before the session.
- Confirm `/admin/audit.csv` rejects unauthenticated access before the session.

## Data And Backup

- Back up the SQLite database file before the session.
- Record the database path from `DEMO_PREDICTION_DB`.
- Keep the backup separate from the working database.
- Decide before the session whether demo data will be deleted or retained after the session.
- If retaining data, record the retention owner, retention period, and access location.

## Demo Flow Checks

- Start the app on `8093`, or use `8094`, `8095`, or `8096` only if `8093` is unavailable.
- Open the dashboard and confirm displayable markets render.
- Switch between two participant codes and confirm balances and positions remain separate.
- Open a market detail page and confirm the non-cash demo point notice is visible.
- Create one demo participation with a low point amount.
- Open `マイスコア`, `デモポジション`, and `結果確認`.
- Confirm result rows explain pending, confirmed, or held states clearly.

## Result Confirmation Checks

- Before running result confirmation, review pending rows in `結果確認`.
- Run result confirmation only with the management code.
- After confirmation, review `結果確認` for reflected score, reason, reference label, and confirmation time.
- If a result is held or unclear, do not manually alter the database during the demo.
- If an operational mistake occurs, record it for follow-up. Correction, reversal-entry, and re-settlement workflows are not implemented.

## Audit CSV Checks

- Open `/admin/audit` with the management code.
- Apply participant and date range filters before export.
- Export operation records, demo point history, and result records from `/admin/audit.csv`.
- Store exported CSV files in the agreed internal location.
- Treat CSV files as internal operational records.

## End-Of-Demo Checks

- Stop the app.
- Export final audit CSV files if data will be retained.
- Back up or delete the SQLite database according to the pre-decided policy.
- Remove temporary participant-code distribution notes.
- Rotate `DEMO_ADMIN_TOKEN` before the next external collaborator session.
