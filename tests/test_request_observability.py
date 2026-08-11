import asyncio
import json
import logging
import re
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest

import app.main as main
import app.observability as observability
from app.collateral_ledger import POINT_SCALE, bootstrap_v2_point_supply
from app.storage import get_settlement_by_position_id, replace_markets


def _request_logs(caplog):
    return [json.loads(record.message) for record in caplog.records if record.name == "app.http"]


@pytest.mark.parametrize(
    ("value", "accepted"),
    [
        ("123e4567-e89b-12d3-a456-426614174000", True),
        ("custom.trace_1:segment", True),
        (None, False),
        ("", False),
        ("contains space", False),
        ("a" * 129, False),
        ("日本語", False),
    ],
)
def test_request_id_normalization(value, accepted):
    request_id = observability.normalize_request_id(value)
    if accepted:
        assert request_id == value
    else:
        assert request_id != value
        assert re.fullmatch(r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}", request_id)


def test_request_id_control_character_is_replaced():
    request_id = observability.normalize_request_id("bad\nrequest-id")
    assert re.fullmatch(r"[0-9a-f-]{36}", request_id)


def test_health_response_and_structured_log_use_canonical_id(client, caplog):
    caplog.set_level(logging.INFO, logger="app.http")
    response = client.get("/health", headers={"X-Request-ID": "health-request-123"})
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "ok": True}
    assert response.headers["X-Request-ID"] == "health-request-123"
    logs = _request_logs(caplog)
    assert len(logs) == 1
    assert logs[0] == {
        "event": "http_request",
        "request_id": "health-request-123",
        "method": "GET",
        "path": "/health",
        "status_code": 200,
        "duration_ms": logs[0]["duration_ms"],
    }
    assert logs[0]["duration_ms"] >= 0


def test_request_id_headers_cover_http_errors(client):
    forbidden = client.post("/api/auth/logout", auto_security=False, headers={"X-Request-ID": "forbidden-request"})
    missing = client.get("/missing", headers={"X-Request-ID": "missing-request"})
    unauthenticated = client.get("/api/auth/me", headers={"X-Request-ID": "unauthenticated-request"})
    assert forbidden.status_code == 403
    assert forbidden.headers["X-Request-ID"] == "forbidden-request"
    assert missing.status_code == 404
    assert missing.headers["X-Request-ID"] == "missing-request"
    assert unauthenticated.status_code == 401
    assert unauthenticated.headers["X-Request-ID"] == "unauthenticated-request"


def test_request_id_headers_cover_existing_400_409_429_and_503_responses(client, db_conn, sample_markets):
    invalid = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "MAYBE", "stake": 1},
        headers={"X-Request-ID": "bad-request"},
    )
    assert invalid.status_code == 400
    assert invalid.headers["X-Request-ID"] == "bad-request"

    bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="header-bootstrap")
    payload = {"participant_id": "participant-1", "amount_micro": POINT_SCALE, "idempotency_key": "header-allocation"}
    client.post("/api/admin/v2/point-allocations", json=payload, headers={"x-demo-admin-token": "test-admin"})
    conflict = client.post(
        "/api/admin/v2/point-allocations",
        json={**payload, "amount_micro": 1},
        headers={"X-Request-ID": "conflict-request", "x-demo-admin-token": "test-admin"},
    )
    assert conflict.status_code == 409
    assert conflict.headers["X-Request-ID"] == "conflict-request"

    responses = [
        client.post(
            "/api/demo/predict",
            json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 1},
            headers={"X-Request-ID": f"rate-request-{index}"},
        )
        for index in range(4)
    ]
    assert responses[-1].status_code == 429
    assert responses[-1].headers["X-Request-ID"] == "rate-request-3"

    db_conn.execute("pragma user_version = 0")
    unavailable = client.get("/ready", headers={"X-Request-ID": "ready-request"})
    assert unavailable.status_code == 503
    assert unavailable.headers["X-Request-ID"] == "ready-request"


def test_invalid_request_id_and_request_secrets_are_not_logged(client, caplog):
    caplog.set_level(logging.INFO, logger="app.http")
    response = client.post(
        "/api/auth/login?token=query-secret-sentinel",
        json={"email": "email-secret-sentinel@example.test", "password": "password-secret-sentinel"},
        auto_security=False,
        headers={
            "X-Request-ID": "invalid-request-id-sentinel!",
            "Authorization": "Bearer session-secret-sentinel",
            "Cookie": "demo_csrf=csrf-secret-sentinel; demo_admin_token=admin-secret-sentinel",
        },
    )
    assert response.status_code == 401
    assert response.headers["X-Request-ID"] != "invalid-request-id-sentinel!"
    output = "\n".join(record.message for record in caplog.records)
    for secret in (
        "query-secret-sentinel", "email-secret-sentinel", "password-secret-sentinel",
        "session-secret-sentinel", "csrf-secret-sentinel", "admin-secret-sentinel",
        "invalid-request-id-sentinel",
    ):
        assert secret not in output


def test_logging_failure_isolated(client, monkeypatch):
    monkeypatch.setattr(observability.logger, "info", lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("log failure")))
    response = client.get("/health")
    assert response.status_code == 200
    assert response.headers["X-Request-ID"]


def test_operation_rejection_uses_canonical_request_id(client):
    main._operation_rejections.clear()
    response = client.post("/api/auth/logout", auto_security=False, headers={"X-Request-ID": "rejection-request-123"})
    assert response.status_code == 403
    assert main._operation_rejections[-1]["request_id"] == "rejection-request-123"


def test_unhandled_exception_is_generic_correlated_and_logged_once(client, caplog, monkeypatch):
    caplog.set_level(logging.INFO, logger="app.http")
    route = next(route for route in main.app.routes if getattr(route, "path", None) == "/health")

    async def fail():
        raise RuntimeError("exception-secret-sentinel")

    monkeypatch.setattr(route.dependant, "call", fail)
    response = client.get("/health", headers={"X-Request-ID": "failure-request-123"})
    assert response.status_code == 500
    assert response.json() == {"detail": "internal server error"}
    assert response.headers["X-Request-ID"] == "failure-request-123"
    logs = _request_logs(caplog)
    assert len(logs) == 1
    assert logs[0]["status_code"] == 500
    assert logs[0]["error_type"] == "RuntimeError"
    assert "exception-secret-sentinel" not in "\n".join(record.message for record in caplog.records)


def test_request_id_propagates_to_prediction_and_auth_audit(client, db_conn, sample_markets):
    prediction_id = "prediction-request-123"
    prediction = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 10},
        headers={"X-Request-ID": prediction_id},
    )
    assert prediction.status_code == 200
    assert db_conn.execute("select request_id from simulated_orders order by id desc limit 1").fetchone()[0] == prediction_id
    assert db_conn.execute("select request_id from demo_point_ledger order by id desc limit 1").fetchone()[0] == prediction_id

    auth_id = "auth-request-123"
    login = client.post(
        "/api/auth/login",
        json={"email": "missing@example.test", "password": "password-secret"},
        auto_security=False,
        headers={"X-Request-ID": auth_id},
    )
    assert login.status_code == 401
    assert db_conn.execute("select request_id from demo_audit_events where event_type = 'login_failed' order by id desc limit 1").fetchone()[0] == auth_id


def test_request_id_propagates_to_v2_allocation(client, db_conn):
    bootstrap_v2_point_supply(db_conn, amount_micro=POINT_SCALE, idempotency_key="observability-bootstrap")
    request_id = "v2-request-123"
    response = client.post(
        "/api/admin/v2/point-allocations",
        json={"participant_id": "participant-1", "amount_micro": POINT_SCALE, "idempotency_key": "observability-allocation"},
        headers={"X-Request-ID": request_id, "x-demo-admin-token": "test-admin"},
    )
    assert response.status_code == 200
    assert db_conn.execute("select request_id from point_allocation_events order by id desc limit 1").fetchone()[0] == request_id
    assert db_conn.execute("select request_id from collateral_ledger_entries where reference_type = 'point_allocation_event' order by id desc limit 1").fetchone()[0] == request_id


def test_request_id_propagates_through_settlement_ledger_and_audit(client, db_conn, sample_markets):
    prediction = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 10},
    )
    settlement = get_settlement_by_position_id(db_conn, prediction.json()["position"]["id"])
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])
    request_id = "settlement-request-123"
    response = client.post("/api/demo/settle", headers={"X-Request-ID": request_id})
    assert response.status_code == 200
    assert db_conn.execute(
        "select request_id from demo_point_ledger where reference_type = 'demo_settlement' and reference_id = ?",
        (str(settlement["id"]),),
    ).fetchone()[0] == request_id
    assert db_conn.execute(
        "select request_id from demo_audit_events where event_type = 'settlement_checked' and reference_id = ?",
        (str(settlement["id"]),),
    ).fetchone()[0] == request_id


def test_concurrent_request_ids_do_not_cross_contaminate():
    request_ids = ["concurrent-a", "concurrent-b"]

    def get_health(request_id):
        async def send():
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as http_client:
                return await http_client.get("/health", headers={"X-Request-ID": request_id})

        return asyncio.run(send())

    with ThreadPoolExecutor(max_workers=2) as executor:
        responses = list(executor.map(get_health, request_ids))
    assert [response.headers["X-Request-ID"] for response in responses] == request_ids
