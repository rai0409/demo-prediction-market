from dataclasses import replace

import httpx
import pytest

from app.config import Settings
import app.recovery_alert_delivery as delivery
from scripts import run_recovery_alert_notification as cli


URL = "https://webhook.invalid/recovery"


class Response:
    def __init__(self, status_code): self.status_code = status_code


class Client:
    def __init__(self, outcomes): self.outcomes, self.calls = list(outcomes), []
    def post(self, url, **kwargs):
        self.calls.append((url, kwargs)); outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception): raise outcome
        return outcome


@pytest.fixture
def settings():
    return Settings(live=False, poll_seconds=30, limit=50, db_path="unused.sqlite", recovery_alert_webhook_enabled=True, recovery_alert_webhook_url=URL, recovery_alert_webhook_timeout_seconds=7)


@pytest.fixture
def decision():
    return {"notify": True, "action": "notify_warning", "state": "warning", "severity": "warning", "reason_code": "offhost_recovery_degraded", "evaluated_at": "2026-01-01T00:00:00+00:00", "decision_id": "a" * 24, "recovery_health_status": "DEGRADED", "restore_drill_status": "PASS"}


def configure(monkeypatch, value, acknowledgement=None, events=None):
    monkeypatch.setattr(delivery.notification_policy, "decide", lambda: value)
    def acknowledge(decision_id):
        if events is not None: events.append(decision_id)
        return acknowledgement or {"status": "ok", "error_code": None}
    monkeypatch.setattr(delivery.notification_policy, "acknowledge", acknowledge)


def test_no_notification_and_configuration_failures_do_not_send(monkeypatch, settings, decision):
    configure(monkeypatch, {"notify": False, "action": "none", "state": "healthy", "reason_code": "recovery_healthy", "decision_id": None})
    client = Client([])
    assert delivery.run(settings, http_client=client)[0] == 0 and client.calls == []
    configure(monkeypatch, decision)
    for configured in (replace(settings, recovery_alert_webhook_enabled=False), replace(settings, recovery_alert_webhook_url=""), replace(settings, recovery_alert_webhook_url="http://invalid")):
        assert delivery.run(configured, http_client=Client([]))[0] == delivery.EXIT_CONFIGURATION


@pytest.mark.parametrize("outcome", [Response(302), Response(400), Response(429), Response(500), httpx.ReadTimeout("timeout"), httpx.ConnectError("offline")])
def test_delivery_failures_do_not_acknowledge(monkeypatch, settings, decision, outcome):
    events = []; configure(monkeypatch, decision, events=events); client = Client([outcome])
    assert delivery.run(settings, http_client=client)[0] == delivery.EXIT_DELIVERY
    assert events == [] and client.calls[0][1]["follow_redirects"] is False


def test_success_ack_error_and_secret_safe_allowlist(monkeypatch, settings, decision):
    events = []; configure(monkeypatch, {**decision, "secret": "do-not-send"}, events=events); client = Client([Response(204)])
    code, result = delivery.run(settings, http_client=client)
    assert code == 0 and result["acknowledged"] is True and events == ["a" * 24]
    assert set(client.calls[0][1]["json"]) == set(delivery.PAYLOAD_FIELDS)
    assert URL not in str(result) and "do-not-send" not in str(client.calls[0][1]["json"])
    configure(monkeypatch, decision, acknowledgement={"status": "error", "error_code": "state_error"})
    assert delivery.run(settings, http_client=Client([Response(200)]))[0] == delivery.EXIT_ACKNOWLEDGEMENT


def test_invalid_decision_never_sends(monkeypatch, settings, decision):
    configure(monkeypatch, {**decision, "decision_id": "invalid"}); client = Client([])
    assert delivery.run(settings, http_client=client)[0] == delivery.EXIT_DECISION and client.calls == []


def test_cli_json_is_safe_and_has_no_database_dependency(monkeypatch, settings, capsys):
    monkeypatch.setattr(cli, "get_settings", lambda: settings)
    monkeypatch.setattr(cli, "run", lambda configured: (0, {"status": "no_notification", "notify": False, "delivered": False, "acknowledged": False, "action": "none", "state": "healthy", "reason_code": "recovery_healthy", "decision_id": None, "error_code": None}))
    assert cli.main(["--json"]) == 0
    assert 'webhook.invalid' not in capsys.readouterr().out



@pytest.mark.parametrize(
    "changes",
    [
        {"state": "healthy", "action": "notify_critical", "severity": "critical"},
        {"state": "warning", "action": "notify_critical"},
        {"state": "warning", "severity": "critical"},
        {"state": "critical", "severity": "warning"},
        {"state": "check_error", "action": "notify_warning"},
    ],
)
def test_inconsistent_action_state_or_severity_never_sends(
    monkeypatch,
    settings,
    decision,
    changes,
):
    invalid = {**decision, **changes}
    configure(monkeypatch, invalid)
    client = Client([])

    code, result = delivery.run(settings, http_client=client)

    assert code == delivery.EXIT_DECISION
    assert result["error_code"] == "invalid_decision"
    assert client.calls == []
