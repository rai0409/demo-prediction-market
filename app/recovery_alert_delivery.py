"""Deliver recovery-alert decisions to a generic HTTPS webhook."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlparse

import httpx

from app import recovery_alert_notification_policy as notification_policy
from app.config import Settings


EXIT_OK = 0
EXIT_CONFIGURATION = 2
EXIT_DECISION = 3
EXIT_DELIVERY = 4
EXIT_ACKNOWLEDGEMENT = 5
SCHEMA_VERSION = 1
SOURCE = "demo_prediction_market_recovery"
USER_AGENT = "demo-prediction-market-recovery-alert/1"
PAYLOAD_FIELDS = (
    "schema_version", "source", "action", "state", "severity", "reason_code", "evaluated_at", "decision_id",
    "recovery_health_status", "restore_drill_status",
)
_DECISION_ID = re.compile(r"^[0-9a-f]{24}$")
_EXPECTED_ACTION = {
    "healthy": "notify_recovery",
    "warning": "notify_warning",
    "critical": "notify_critical",
    "check_error": "notify_check_error",
}
_EXPECTED_SEVERITY = {
    "healthy": "none",
    "warning": "warning",
    "critical": "critical",
    "check_error": "critical",
}


def _result(*, status: str, notify: bool, delivered: bool, acknowledged: bool, decision: dict[str, Any], error_code: str | None) -> dict[str, Any]:
    return {"status": status, "notify": notify, "delivered": delivered, "acknowledged": acknowledged, "action": decision.get("action"), "state": decision.get("state"), "reason_code": decision.get("reason_code"), "decision_id": decision.get("decision_id"), "error_code": error_code}


def _valid_webhook_url(url: str) -> bool:
    parsed = urlparse(url)
    return parsed.scheme == "https" and bool(parsed.hostname)


def _payload(decision: dict[str, Any]) -> dict[str, Any]:
    return {"schema_version": SCHEMA_VERSION, "source": SOURCE, **{field: decision[field] for field in PAYLOAD_FIELDS if field not in {"schema_version", "source"}}}


def _valid_notification_decision(decision: dict[str, Any]) -> bool:
    required = set(PAYLOAD_FIELDS) - {"schema_version", "source"}
    return (
        required <= set(decision)
        and decision["action"] in notification_policy.ACTIONS - {"none"}
        and decision["state"] in notification_policy.STATES
        and decision["action"] == _EXPECTED_ACTION.get(decision["state"])
        and decision["severity"] == _EXPECTED_SEVERITY.get(decision["state"])
        and isinstance(decision["reason_code"], str)
        and isinstance(decision["evaluated_at"], str)
        and isinstance(decision["recovery_health_status"], (str, type(None)))
        and isinstance(decision["restore_drill_status"], (str, type(None)))
        and isinstance(decision["decision_id"], str)
        and bool(_DECISION_ID.fullmatch(decision["decision_id"]))
    )


def run(settings: Settings, *, http_client: Any | None = None) -> tuple[int, dict[str, Any]]:
    """Evaluate, deliver exactly once, and acknowledge only HTTP 2xx success."""
    try:
        decision = notification_policy.decide()
    except Exception:
        decision = {}
        return EXIT_DECISION, _result(status="error", notify=False, delivered=False, acknowledged=False, decision=decision, error_code="decision_error")
    if not isinstance(decision, dict) or decision.get("error_code"):
        safe = decision if isinstance(decision, dict) else {}
        return EXIT_DECISION, _result(status="error", notify=False, delivered=False, acknowledged=False, decision=safe, error_code="decision_error")
    if decision.get("notify") is False:
        return EXIT_OK, _result(status="no_notification", notify=False, delivered=False, acknowledged=False, decision=decision, error_code=None)
    if decision.get("notify") is not True or not _valid_notification_decision(decision):
        return EXIT_DECISION, _result(status="error", notify=False, delivered=False, acknowledged=False, decision=decision, error_code="invalid_decision")
    if not settings.recovery_alert_webhook_enabled:
        return EXIT_CONFIGURATION, _result(status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_disabled")
    if not settings.recovery_alert_webhook_url:
        return EXIT_CONFIGURATION, _result(status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_url_missing")
    if not _valid_webhook_url(settings.recovery_alert_webhook_url):
        return EXIT_CONFIGURATION, _result(status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_url_invalid")
    client = http_client or httpx.Client(follow_redirects=False)
    try:
        response = client.post(settings.recovery_alert_webhook_url, json=_payload(decision), headers={"Content-Type": "application/json", "User-Agent": USER_AGENT}, timeout=settings.recovery_alert_webhook_timeout_seconds, follow_redirects=False)
    except httpx.TimeoutException:
        return EXIT_DELIVERY, _result(status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_timeout")
    except httpx.RequestError:
        return EXIT_DELIVERY, _result(status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_request_error")
    finally:
        if http_client is None: client.close()
    if not 200 <= response.status_code <= 299:
        return EXIT_DELIVERY, _result(status="error", notify=True, delivered=False, acknowledged=False, decision=decision, error_code="webhook_http_error")
    try:
        acknowledgement = notification_policy.acknowledge(decision["decision_id"])
    except Exception:
        acknowledgement = {}
    if not isinstance(acknowledgement, dict) or acknowledgement.get("error_code") or acknowledgement.get("status") != "ok":
        return EXIT_ACKNOWLEDGEMENT, _result(status="error", notify=True, delivered=True, acknowledged=False, decision=decision, error_code="acknowledgement_error")
    return EXIT_OK, _result(status="delivered", notify=True, delivered=True, acknowledged=True, decision=decision, error_code=None)
