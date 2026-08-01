"""Deliver pending sync-alert notification decisions to a generic HTTPS webhook."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app import sync_alert_notification_policy as notification_policy
from app.config import Settings


EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_DECISION = 3
EXIT_DELIVERY = 4
EXIT_ACKNOWLEDGEMENT = 5

SCHEMA_VERSION = 1
SOURCE = "demo_prediction_market_sync"
USER_AGENT = "demo-prediction-market-sync-alert/1"
PAYLOAD_FIELDS = (
    "schema_version",
    "source",
    "action",
    "state",
    "severity",
    "reason_code",
    "freshness_status",
    "last_sync_success_at",
    "last_sync_status",
    "consecutive_failures",
    "evaluated_at",
    "decision_id",
)
_DECISION_ID = re.compile(r"^[0-9a-f]{24}$")


def _result(
    *,
    status: str,
    notify: bool,
    delivered: bool,
    acknowledged: bool,
    decision: dict[str, Any],
    error_code: str | None,
) -> dict[str, Any]:
    return {
        "status": status,
        "notify": notify,
        "delivered": delivered,
        "acknowledged": acknowledged,
        "action": decision.get("action"),
        "state": decision.get("state"),
        "reason_code": decision.get("reason_code"),
        "decision_id": decision.get("decision_id"),
        "error_code": error_code,
    }


def _valid_webhook_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.netloc)


def _payload(decision: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "source": SOURCE,
        "action": decision["action"],
        "state": decision["state"],
        "severity": decision["severity"],
        "reason_code": decision["reason_code"],
        "freshness_status": decision["freshness_status"],
        "last_sync_success_at": decision["last_sync_success_at"],
        "last_sync_status": decision["last_sync_status"],
        "consecutive_failures": decision["consecutive_failures"],
        "evaluated_at": decision["evaluated_at"],
        "decision_id": decision["decision_id"],
    }


def _valid_notification_decision(decision: dict[str, Any]) -> bool:
    required = set(PAYLOAD_FIELDS) - {"schema_version", "source"}
    return (
        required <= set(decision)
        and decision["action"] in notification_policy.ACTIONS - {"none"}
        and decision["state"] in notification_policy.STATES
        and isinstance(decision["decision_id"], str)
        and bool(_DECISION_ID.fullmatch(decision["decision_id"]))
    )


def run(
    conn: Any,
    settings: Settings,
    *,
    http_client: Any | None = None,
) -> tuple[int, dict[str, Any]]:
    """Decide, deliver once, then acknowledge only a successful delivery."""
    try:
        decision = notification_policy.decide(conn, settings.db_path)
    except Exception:
        decision = {}
        return EXIT_DECISION, _result(
            status="error", notify=False, delivered=False, acknowledged=False, decision=decision, error_code="decision_error"
        )

    if not isinstance(decision, dict) or decision.get("error_code"):
        safe_decision = decision if isinstance(decision, dict) else {}
        return EXIT_DECISION, _result(
            status="error", notify=False, delivered=False, acknowledged=False, decision=safe_decision, error_code="decision_error"
        )
    if decision.get("notify") is False:
        return EXIT_OK, _result(
            status="no_notification", notify=False, delivered=False, acknowledged=False, decision=decision, error_code=None
        )
    if decision.get("notify") is not True or not _valid_notification_decision(decision):
        return EXIT_DECISION, _result(
            status="error", notify=False, delivered=False, acknowledged=False, decision=decision, error_code="invalid_decision"
        )
    if not settings.sync_alert_webhook_enabled:
        return EXIT_CONFIGURATION, _result(
            status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_disabled"
        )
    if not settings.sync_alert_webhook_url:
        return EXIT_CONFIGURATION, _result(
            status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_url_missing"
        )
    if not _valid_webhook_url(settings.sync_alert_webhook_url):
        return EXIT_CONFIGURATION, _result(
            status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_url_invalid"
        )

    client = http_client or httpx.Client(follow_redirects=False)
    try:
        response = client.post(
            settings.sync_alert_webhook_url,
            json=_payload(decision),
            headers={"Content-Type": "application/json", "User-Agent": USER_AGENT},
            timeout=settings.sync_alert_webhook_timeout_seconds,
            follow_redirects=False,
        )
    except httpx.TimeoutException:
        return EXIT_DELIVERY, _result(
            status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_timeout"
        )
    except httpx.RequestError:
        return EXIT_DELIVERY, _result(
            status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_request_error"
        )
    finally:
        if http_client is None:
            client.close()

    if not 200 <= response.status_code <= 299:
        return EXIT_DELIVERY, _result(
            status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_http_error"
        )
    try:
        acknowledgement = notification_policy.acknowledge(settings.db_path, decision["decision_id"])
    except Exception:
        acknowledgement = {}
    if not isinstance(acknowledgement, dict) or acknowledgement.get("error_code") or acknowledgement.get("status") != "ok":
        return EXIT_ACKNOWLEDGEMENT, _result(
            status="error", notify=True, delivered=True, acknowledged=False, decision=decision, error_code="acknowledgement_error"
        )
    return EXIT_OK, _result(
        status="delivered", notify=True, delivered=True, acknowledged=True, decision=decision, error_code=None
    )
