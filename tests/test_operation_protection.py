from dataclasses import replace
import re

import app.main as main
from app.storage import INITIAL_DEMO_POINTS, get_balance


def test_internal_point_add_rejected_without_admin_token_header(client):
    response = client.post(
        "/api/demo/wallet/add-points",
        json={"amount": 100, "reason": "調整"},
        auto_admin=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "内部操作は許可されていません。"


def test_internal_point_add_rejected_when_admin_token_is_not_configured(client, monkeypatch):
    monkeypatch.setattr(main, "settings", replace(main.settings, admin_token=""))

    response = client.post(
        "/api/demo/wallet/add-points",
        headers={"x-demo-admin-token": "test-admin"},
        json={"amount": 100, "reason": "調整"},
        auto_admin=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "内部操作は現在利用できません。"


def test_internal_point_add_allowed_with_admin_token(client):
    response = client.post(
        "/api/demo/wallet/add-points",
        headers={"x-demo-admin-token": "test-admin", "x-demo-user": "alice"},
        json={"amount": 100, "reason": "調整"},
        auto_admin=False,
    )

    assert response.status_code == 200
    assert response.json()["balance"] == INITIAL_DEMO_POINTS + 100


def test_internal_diagnostics_require_admin_token(client):
    debug_response = client.get("/api/debug/source-status")
    include_all_response = client.get("/api/markets?include_all=true")
    candidates_response = client.get("/api/demo/resolution-candidates")

    assert debug_response.status_code == 403
    assert include_all_response.status_code == 403
    assert candidates_response.status_code == 403


def test_internal_diagnostics_reject_query_admin_token(client):
    response = client.get("/api/debug/source-status?admin_token=test-admin")

    assert response.status_code == 403


def test_post_without_csrf_token_is_rejected(client, sample_markets):
    response = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 10},
        auto_security=False,
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "操作を確認できませんでした。ページを再読み込みしてください。"


def test_post_with_csrf_token_is_allowed(client, sample_markets):
    response = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 10},
    )

    assert response.status_code == 200
    assert response.json()["balance"] == INITIAL_DEMO_POINTS - 10


def test_rendered_csrf_token_matches_cookie(client):
    response = client.get("/")
    token = response.cookies["demo_csrf"]

    assert f'data-csrf-token="{token}"' in response.text
    assert 'action="/demo-user' not in response.text


def test_secure_cookie_setting_adds_secure_attribute(client, monkeypatch):
    from dataclasses import replace

    import app.main as main

    monkeypatch.setattr(main, "settings", replace(
            main.settings,
            cookie_secure=True,
            allow_demo_user_header=True,
        ))

    page = client.get("/", headers={"x-demo-user": "secure-user"})
    token = page.cookies["demo_csrf"]
    admin = client.post(
        f"/admin/audit/access?csrf_token={token}",
        data={"admin_token": "test-admin"},
        auto_security=False,
    )

    set_cookie = "\n".join(page.headers.get_list("set-cookie") + admin.headers.get_list("set-cookie"))
    assert "demo_csrf=" in set_cookie
    assert "demo_user_id=secure-user" in set_cookie
    assert "demo_admin_token=test-admin" in set_cookie
    assert set_cookie.count("Secure") >= 3


def test_demo_participant_code_is_normalized(client):
    response = client.get("/api/demo/balance", headers={"x-demo-user": "alice///bob??"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "alice-bob"


def test_empty_symbol_only_participant_code_uses_default(client):
    response = client.get("/api/demo/balance", headers={"x-demo-user": "!!!"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "participant-1"


def test_demo_participation_rate_limit(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    responses = [
        client.post(
            "/api/demo/predict",
            headers={"x-demo-user": "rate-user"},
            json={"market_id": market_id, "outcome": "YES", "stake": 1},
        )
        for _ in range(4)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200, 429]
    assert get_balance(db_conn, "rate-user") == INITIAL_DEMO_POINTS - 3


def test_demo_participation_stake_limit(client, sample_markets):
    response = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 10001},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "stake is above the allowed demo point limit"
