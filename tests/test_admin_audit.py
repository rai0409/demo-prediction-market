from app.storage import replace_markets


def _join(client, user_id, market_id, stake=20):
    return client.post(
        "/api/demo/predict",
        headers={"x-demo-user": user_id},
        json={"market_id": market_id, "outcome": "YES", "stake": stake},
    )


def test_admin_audit_rejects_without_admin_code(client):
    response = client.get("/admin/audit")

    assert response.status_code == 403
    assert "管理コード確認" in response.text
    assert "操作記録" not in response.text


def test_admin_audit_rejects_query_admin_code(client):
    response = client.get("/admin/audit?admin_token=test-admin")

    assert response.status_code == 403
    assert "管理コード確認" in response.text
    assert "操作記録" not in response.text


def test_admin_audit_renders_records_with_admin_header(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _join(client, "alice", market_id, stake=30)
    client.post(
        "/api/demo/wallet/add-points",
        headers={"x-demo-user": "alice"},
        json={"amount": 100, "reason": "調整"},
    )
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])
    client.post("/api/demo/settle", headers={"x-demo-user": "alice"})

    response = client.get("/admin/audit", headers={"x-demo-admin-token": "test-admin"})

    assert response.status_code == 200
    html = response.text
    assert "内部確認" in html
    assert "参加者概要" in html
    assert "操作記録" in html
    assert "デモポイント履歴" in html
    assert "結果記録" in html
    assert "拒否・異常操作" in html
    assert "alice" in html
    assert "デモ参加記録" in html
    assert "デモポイント調整" in html
    assert "的中" in html


def test_admin_audit_participant_filter(client, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _join(client, "alice", market_id, stake=30)
    _join(client, "bob", market_id, stake=40)

    response = client.get("/admin/audit?participant=alice", headers={"x-demo-admin-token": "test-admin"})

    assert response.status_code == 200
    assert "alice" in response.text
    assert "bob" not in response.text


def test_admin_audit_access_form_sets_cookie(client):
    token = client.get("/admin/audit").cookies["demo_csrf"]

    response = client.post(
        f"/admin/audit/access?csrf_token={token}",
        data={"admin_token": "test-admin"},
        auto_security=False,
    )

    assert response.status_code == 303
    assert "demo_admin_token" in response.headers["set-cookie"]


def test_admin_audit_page_does_not_add_forbidden_terms(client):
    response = client.get("/admin/audit", headers={"x-demo-admin-token": "test-admin"})

    assert response.status_code == 200
    assert "換金可能" not in response.text
    assert "Polymarket公式" not in response.text


def test_admin_audit_csv_rejects_without_admin_code(client):
    response = client.get("/admin/audit.csv?type=audit")

    assert response.status_code == 403


def test_admin_audit_csv_rejects_query_admin_code(client):
    response = client.get("/admin/audit.csv?type=audit&admin_token=test-admin")

    assert response.status_code == 403


def test_admin_audit_csv_exports_with_admin_code(client, sample_markets):
    _join(client, "alice", sample_markets[0]["market_id"], stake=30)

    response = client.get("/admin/audit.csv?type=audit", headers={"x-demo-admin-token": "test-admin"})

    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "operation_label" in response.text
    assert "デモ参加記録" in response.text
    assert "alice" in response.text


def test_admin_audit_csv_participant_filter(client, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _join(client, "alice", market_id, stake=30)
    _join(client, "bob", market_id, stake=40)

    response = client.get(
        "/admin/audit.csv?type=ledger&participant=alice",
        headers={"x-demo-admin-token": "test-admin"},
    )

    assert response.status_code == 200
    assert "alice" in response.text
    assert "bob" not in response.text


def test_admin_audit_date_filter_applies_to_page_and_csv(client, sample_markets):
    _join(client, "alice", sample_markets[0]["market_id"], stake=30)

    page = client.get(
        "/admin/audit?date_from=2999-01-01",
        headers={"x-demo-admin-token": "test-admin"},
    )
    csv_response = client.get(
        "/admin/audit.csv?type=audit&date_from=2999-01-01",
        headers={"x-demo-admin-token": "test-admin"},
    )

    assert page.status_code == 200
    assert "デモ参加記録" not in page.text
    assert "デモ参加記録" not in csv_response.text


def test_admin_audit_limit_is_capped(client, sample_markets):
    market_id = sample_markets[0]["market_id"]
    for index in range(4):
        _join(client, f"user-{index}", market_id, stake=1)

    response = client.get(
        "/admin/audit?limit=2",
        headers={"x-demo-admin-token": "test-admin"},
    )
    too_large = client.get(
        "/admin/audit?limit=999",
        headers={"x-demo-admin-token": "test-admin"},
    )

    assert response.status_code == 200
    assert "現在 1 ページ目" in response.text
    assert response.text.count("デモ参加記録") <= 2
    assert 'value="200"' in too_large.text


def test_admin_audit_csv_injection_cells_are_sanitized(client):
    client.post(
        "/api/demo/wallet/add-points",
        headers={"x-demo-user": "alice"},
        json={"amount": 100, "reason": "=unsafe"},
    )

    response = client.get(
        "/admin/audit.csv?type=ledger&participant=alice",
        headers={"x-demo-admin-token": "test-admin"},
    )

    assert response.status_code == 200
    assert "'=unsafe" in response.text
    assert ",=unsafe" not in response.text
