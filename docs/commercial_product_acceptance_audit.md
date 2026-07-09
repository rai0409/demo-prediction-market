# Commercial Product Acceptance Audit

Status: accepted with local-demo limitations.

Scope inspected:
- `/` market list and `全マーケットを見る` navigation
- `/markets/{market_id}` market detail and demo participation panel
- `/demo-wallet` score balance and local ledger
- `/demo-positions` participation history
- `/demo-results` result transparency and score reflection
- `/admin/audit` protected internal review screen
- Public JSON endpoints used by the rendered product UI

Acceptance checks:
- route coverage includes market list, market detail, voting, wallet/ledger, result transparency, and protected audit review.
- Normal navigation lands on rendered product pages, not raw JSON/API text.
- Market list and detail pages use product wording for status and updates.
- Public UI does not show transport or developer terms such as REST, WebSocket, API state, polling, freshness layer, debug, raw, or 30-second polling details.
- Japanese is the default core flow; English appears only when the language toggle selects English.
- List refresh updates summary/status only; detail refresh updates only the visible market.
- Demo point balance, local ledger, participation history, and result rows stay participant-scoped.
- Result transparency explains reflected scores in plain language and does not imply real-money settlement.
- Protected audit review remains unlinked from public navigation and requires the management code.

Remaining limitations:
- Participant codes are local demo identifiers, not production authentication.
- Internal audit logs are SQLite records, not tamper-resistant production audit storage.
- This is still a local demo; legal, compliance, operational monitoring, and production security review are required before any external deployment.
