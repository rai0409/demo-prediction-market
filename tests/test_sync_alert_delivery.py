from __future__ import annotations

from dataclasses import replace

import httpx
import pytest

from app.config import Settings
import app.sync_alert_delivery as delivery


WEBHOOK_URL = "https://webhook.invalid/notify"
SECRET_TOKEN = "test-token"


class Response:
    def __init__(self, status_code: int):
        self.status_code = status_code


class Client:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


@pytest.fixture()
def settings():
    return Settings(
        live=False, poll_seconds=30, limit=50, db_path="test.sqlite", sync_alert_webhook_enabled=True,
        sync_alert_webhook_url=WEBHOOK_URL, sync_alert_webhook_timeout_seconds=7,
    )


@pytest.fixture()
def decision():
    return {
        "notify": True, "action": "notify_warning", "state": "warning", "severity": "warning",
        "reason_code": "sync_stale", "freshness_status": "stale", "last_sync_success_at": "2026-01-01T00:00:00+00:00",
        "last_sync_status": "upstream_error", "consecutive_failures": 3, "evaluated_at": "2026-01-01T01:00:00+00:00",
        "decision_id": "a" * 24,
    }


def _set_policy(monkeypatch, decision, acknowledgement=None, events=None):
    monkeypatch.setattr(delivery.notification_policy, "decide", lambda conn, db_path: decision)

    def acknowledge(db_path, decision_id):
        if events is not None:
            events.append("acknowledge")
        return {"status": "ok", "error_code": None} if acknowledgement is None else acknowledgement

    monkeypatch.setattr(delivery.notification_policy, "acknowledge", acknowledge)


def test_no_notification_does_not_send_or_acknowledge(monkeypatch, settings):
    events = []
    _set_policy(
        monkeypatch,
        {"notify": False, "action": "none", "state": "healthy", "reason_code": "healthy", "decision_id": None},
        events=events,
    )
    client = Client([])
    code, result = delivery.run(object(), settings, http_client=client)
    assert code == 0 and result["status"] == "no_notification"
    assert client.calls == [] and events == []


@pytest.mark.parametrize("configured", [(False, WEBHOOK_URL, "webhook_disabled"), (True, "", "webhook_url_missing")])
def test_unavailable_webhook_does_not_send_or_acknowledge(monkeypatch, settings, decision, configured):
    enabled, url, error_code = configured
    events = []
    _set_policy(monkeypatch, decision, events=events)
    code, result = delivery.run(object(), replace(settings, sync_alert_webhook_enabled=enabled, sync_alert_webhook_url=url), http_client=Client([]))
    assert code == 2 and result["error_code"] == error_code and events == []


def test_2xx_sends_once_then_acknowledges_once(monkeypatch, settings, decision):
    events = []
    _set_policy(monkeypatch, decision, events=events)
    client = Client([Response(204)])
    code, result = delivery.run(object(), settings, http_client=client)
    assert code == 0 and result["status"] == "delivered" and result["acknowledged"] is True
    assert len(client.calls) == 1 and events == ["acknowledge"]


@pytest.mark.parametrize("status_code", [400, 429, 500, 302])
def test_non_2xx_does_not_acknowledge(monkeypatch, settings, decision, status_code):
    events = []
    _set_policy(monkeypatch, decision, events=events)
    client = Client([Response(status_code)])
    code, result = delivery.run(object(), settings, http_client=client)
    assert code == 4 and result["acknowledged"] is False and events == []
    assert client.calls[0][1]["follow_redirects"] is False


@pytest.mark.parametrize("outcome", [httpx.ReadTimeout("timeout"), httpx.ConnectError("offline")])
def test_request_errors_do_not_acknowledge(monkeypatch, settings, decision, outcome):
    events = []
    _set_policy(monkeypatch, decision, events=events)
    code, result = delivery.run(object(), settings, http_client=Client([outcome]))
    assert code == 4 and result["acknowledged"] is False and events == []


def test_acknowledgement_failure_returns_exit_five(monkeypatch, settings, decision):
    _set_policy(monkeypatch, decision, acknowledgement={"status": "error", "error_code": "state_error"})
    code, result = delivery.run(object(), settings, http_client=Client([Response(200)]))
    assert code == 5 and result["delivered"] is True and result["acknowledged"] is False


def test_invalid_decision_id_does_not_send(monkeypatch, settings, decision):
    _set_policy(monkeypatch, {**decision, "decision_id": "not-valid"})
    client = Client([])
    code, result = delivery.run(object(), settings, http_client=client)
    assert code == 3 and result["error_code"] == "invalid_decision" and client.calls == []


def test_payload_allowlist_and_secret_safe_result(monkeypatch, settings, decision):
    _set_policy(monkeypatch, {**decision, "db_path": "/private", "token": SECRET_TOKEN})
    client = Client([Response(200)])
    _, result = delivery.run(object(), settings, http_client=client)
    payload = client.calls[0][1]["json"]
    assert set(payload) == set(delivery.PAYLOAD_FIELDS)
    assert WEBHOOK_URL not in str(payload)
    assert SECRET_TOKEN not in str(payload)
    assert WEBHOOK_URL not in str(result)
    assert SECRET_TOKEN not in str(result)


def test_reused_pending_decision_sends_once_per_run(monkeypatch, settings, decision):
    _set_policy(monkeypatch, decision)
    first = Client([Response(503)])
    second = Client([Response(503)])
    assert delivery.run(object(), settings, http_client=first)[0] == 4
    assert delivery.run(object(), settings, http_client=second)[0] == 4
    assert len(first.calls) == len(second.calls) == 1
