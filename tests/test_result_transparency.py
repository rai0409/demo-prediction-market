import re

from app.storage import INITIAL_DEMO_POINTS, get_balance, list_ledger, replace_markets


def visible_text(html):
    html = re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    return re.sub(r"<[^>]+>", "", html)


def _join(client, user_id, market_id, stake=20):
    return client.post(
        "/api/demo/predict",
        headers={"x-demo-user": user_id},
        json={"market_id": market_id, "outcome": "YES", "stake": stake},
    )


def test_unconfirmed_result_page_explains_what_is_missing(client, sample_markets):
    _join(client, "alice", sample_markets[0]["market_id"])

    html = client.get("/demo-results", headers={"x-demo-user": "alice"}).text

    assert "明確な結果をまだ確認できていません。" in html
    assert "結果確認待ち" in html
    assert "確定理由" in html
    assert "参照元" in html
    assert "<th>Market</th>" not in html
    assert "<th>Outcome</th>" not in html
    assert "推定デモリターン" not in html
    assert "pending" not in visible_text(html)


def test_settled_result_page_shows_reason_time_and_reference(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _join(client, "alice", market_id)
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])

    response = client.post("/api/demo/settle", headers={"x-demo-user": "alice"})
    html = client.get("/demo-results", headers={"x-demo-user": "alice"}).text

    assert response.status_code == 200
    assert "明確な結果を確認したため、参考スコアへ反映しました。" in html
    assert "参考データで明確に確認" in html
    assert "結果確定日時" in html
    assert "202" in html


def test_settlement_requires_admin_and_csrf(client, sample_markets):
    _join(client, "alice", sample_markets[0]["market_id"])

    no_admin = client.post("/api/demo/settle", headers={"x-demo-user": "alice"}, auto_admin=False)
    no_csrf = client.post(
        "/api/demo/settle",
        headers={"x-demo-user": "alice", "x-demo-admin-token": "test-admin"},
        auto_security=False,
        auto_admin=False,
    )
    ok = client.post("/api/demo/settle", headers={"x-demo-user": "alice"})

    assert no_admin.status_code == 403
    assert no_csrf.status_code == 403
    assert ok.status_code == 200


def test_settlement_route_does_not_double_apply_score(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _join(client, "alice", market_id, stake=100)
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])

    first = client.post("/api/demo/settle", headers={"x-demo-user": "alice"}).json()
    second = client.post("/api/demo/settle", headers={"x-demo-user": "alice"}).json()
    entries = [entry for entry in list_ledger(db_conn, "alice") if entry["entry_type"] == "settlement_win"]

    assert first["settled_win_count"] == 1
    assert second["checked_count"] == 0
    assert len(entries) == 1
    assert get_balance(db_conn, "alice") == INITIAL_DEMO_POINTS - 100 + entries[0]["amount"]


def test_settlement_results_remain_participant_scoped(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _join(client, "alice", market_id, stake=100)
    _join(client, "bob", market_id, stake=200)
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])

    client.post("/api/demo/settle", headers={"x-demo-user": "alice"})
    alice = client.get("/api/demo/results", headers={"x-demo-user": "alice"}).json()
    bob = client.get("/api/demo/results", headers={"x-demo-user": "bob"}).json()

    assert alice["results"][0]["status"] == "settled_win"
    assert bob["results"][0]["status"] == "pending"
    assert alice["results"][0]["stake"] == 100
    assert bob["results"][0]["stake"] == 200
