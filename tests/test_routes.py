from app.storage import INITIAL_DEMO_POINTS
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


def test_debug_source_status_returns_expected_keys(client):
    response = client.get("/api/debug/source-status")
    assert response.status_code == 200
    payload = response.json()
    expected = {
        "live_enabled",
        "configured_limit",
        "configured_poll_seconds",
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
