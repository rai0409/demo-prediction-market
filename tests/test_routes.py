from app.storage import INITIAL_DEMO_POINTS, get_settlement_by_position_id
from app.storage import replace_markets


def test_health(client):
    response = client.get("/health")
    assert response.status_code == 200
    assert response.json()["ok"] is True


def test_api_markets(client):
    response = client.get("/api/markets")
    assert response.status_code == 200
    assert response.json()["count"] >= 3
    payload = response.json()
    assert "total_market_count" in payload
    assert "displayable_market_count" in payload
    assert "hidden_closed_count" in payload
    assert "hidden_inactive_count" in payload
    assert "hidden_expired_count" in payload
    assert "hidden_no_liquidity_count" in payload
    assert "hidden_resolved_probability_count" in payload
    assert "filters_applied" in payload


def test_api_markets_include_all_returns_hidden_markets(client, db_conn, sample_markets):
    closed = dict(sample_markets[0])
    closed["market_id"] = "route-closed-market"
    closed["closed"] = True
    replace_markets(db_conn, [sample_markets[0], closed])
    default_response = client.get("/api/markets").json()
    include_all_response = client.get("/api/markets?include_all=true").json()
    assert default_response["count"] == 1
    assert include_all_response["count"] == 2
    assert include_all_response["hidden_closed_count"] == 1


def test_post_demo_predict(client, sample_markets):
    response = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 50},
    )
    assert response.status_code == 200
    assert response.json()["balance"] == INITIAL_DEMO_POINTS - 50
    assert response.json()["message"] == "デモ参加を記録しました。"
    assert response.json()["position"]["outcome"] == "YES"
    assert response.json()["position"]["estimated_return"] > 0
    assert response.json()["position"]["settlement_status"] == "pending"


def test_post_demo_predict_creates_pending_settlement(client, db_conn, sample_markets):
    response = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 20},
    )
    position_id = response.json()["position"]["id"]
    settlement = get_settlement_by_position_id(db_conn, position_id)
    assert settlement is not None
    assert settlement["status"] == "pending"


def test_api_demo_results_returns_pending_result(client, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 20},
    )
    response = client.get("/api/demo/results")
    assert response.status_code == 200
    payload = response.json()
    assert payload["pending_count"] == 1
    assert payload["settled_count"] == 0
    assert payload["results"][0]["status"] == "pending"
    assert payload["results"][0]["market_title"] == "Tokyo weekend rain forecast"


def test_debug_source_status_returns_expected_keys(client):
    response = client.get("/api/debug/source-status")
    assert response.status_code == 200
    payload = response.json()
    expected = {
        "live_enabled",
        "configured_limit",
        "configured_poll_seconds",
        "auto_refresh_enabled",
        "configured_refresh_seconds",
        "database_path",
        "last_fetch_status",
        "last_fetch_error",
        "last_fetch_at",
        "last_fetch_url",
        "last_http_status",
        "raw_count",
        "normalized_count",
        "fallback_used",
        "market_count",
        "sample_market_count",
        "total_market_count",
        "displayable_market_count",
        "hidden_closed_count",
        "hidden_inactive_count",
        "hidden_expired_count",
        "hidden_no_liquidity_count",
        "hidden_resolved_probability_count",
        "latest_filter_run_at",
        "runtime_status_file_exists",
        "runtime_response_file_exists",
        "runtime_error_file_exists",
    }
    assert expected.issubset(payload.keys())


def test_dashboard_renders_status_metadata(client):
    response = client.get("/")
    assert response.status_code == 200
    html = response.text
    assert "データ状態" in html
    assert "表示中" in html
    assert "取得合計" in html
    assert "非表示" in html
    assert "全マーケットを見る" in html


def test_market_card_includes_predict_for_eligible_market(client):
    html = client.get("/").text
    assert "予想する" in html
    assert "デモ参加可" in html


def test_market_card_includes_block_reason_for_ineligible_when_rendered(client, db_conn, sample_markets):
    closed = dict(sample_markets[0])
    closed["market_id"] = "closed-card-market"
    closed["closed"] = True
    replace_markets(db_conn, [closed])
    html = client.get("/markets/closed-card-market").text
    assert "デモ参加対象外" in html
    assert "終了済み" in html
    assert "id=\"prediction-form\"" not in html


def test_market_detail_renders_demo_panel_for_eligible_market(client):
    html = client.get("/markets/sample-market-tokyo-rain").text
    assert "id=\"prediction-form\"" in html
    assert "デモ参加する" in html
    assert "現在のデモ残高" in html


def test_demo_positions_page_renders_empty_state(client):
    html = client.get("/demo-positions").text
    assert "デモ参加はまだありません。" in html
    assert "マーケットへ" in html


def test_demo_positions_page_renders_positions(client, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 25},
    )
    html = client.get("/demo-positions").text
    assert "Tokyo weekend rain forecast" in html
    assert "結果待ち" in html
    assert "simulated" not in html
    assert "予想履歴" in html


def test_demo_results_page_renders(client, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 20},
    )
    response = client.get("/demo-results")
    assert response.status_code == 200
    html = response.text
    assert "結果確認" in html
    assert "結果待ち" in html
    assert "参加デモポイント" in html


def test_ui_text_uses_required_words(client):
    dashboard = client.get("/").text
    detail = client.get("/markets/sample-market-tokyo-rain").text
    positions = client.get("/demo-positions").text
    combined = dashboard + detail + positions
    assert "予想する" in combined
    assert "デモ参加" in combined
    assert "デモ参加する" in combined
    assert "デモポイント" in combined
    assert "予想履歴" in combined
    assert "デモポジション" in combined
    assert "デモ残高" in combined


def test_app_ui_action_text_avoids_forbidden_labels(client):
    combined = client.get("/").text + client.get("/markets/sample-market-tokyo-rain").text
    assert "賭ける" not in combined
    assert "ベット" not in combined
    assert "Bet now" not in combined
    assert "place bet" not in combined
    assert ">buy<" not in combined
    assert ">sell<" not in combined
