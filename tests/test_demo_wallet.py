from app.storage import INITIAL_DEMO_POINTS, get_balance, list_audit_events, list_ledger, replace_markets


def test_api_demo_wallet_returns_balance_ledger_summary(client):
    response = client.get("/api/demo/wallet")
    assert response.status_code == 200
    payload = response.json()
    assert payload["balance"] == INITIAL_DEMO_POINTS
    assert "ledger" in payload
    assert "audit_events" in payload
    assert payload["summary"]["ledger_count"] >= 1


def test_add_points_increases_local_demo_balance(client):
    response = client.post(
        "/api/demo/wallet/add-points",
        json={"amount": 500, "reason": "デモポイント追加"},
    )
    assert response.status_code == 200
    assert response.json()["balance"] == INITIAL_DEMO_POINTS + 500


def test_add_points_creates_ledger_with_before_after(client, db_conn):
    client.post("/api/demo/wallet/add-points", json={"amount": 250, "reason": "デモポイント追加"})
    entry = list_ledger(db_conn)[0]
    assert entry["entry_type"] == "demo_point_add"
    assert entry["balance_before"] == INITIAL_DEMO_POINTS
    assert entry["balance_after"] == INITIAL_DEMO_POINTS + 250
    assert entry["reference_type"] == "demo_point_wallet"


def test_add_points_creates_audit_event(client, db_conn):
    client.post("/api/demo/wallet/add-points", json={"amount": 250, "reason": "デモポイント追加"})
    event = list_audit_events(db_conn)[0]
    assert event["event_type"] == "demo_point_add_created"
    assert event["reference_type"] == "demo_point_ledger"


def test_add_points_is_idempotent_with_key(client, db_conn):
    payload = {"amount": 250, "reason": "デモポイント追加", "idempotency_key": "add-key-1"}
    first = client.post("/api/demo/wallet/add-points", json=payload).json()
    second = client.post("/api/demo/wallet/add-points", json=payload).json()
    entries = [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "demo_point_add"]
    assert first["balance"] == INITIAL_DEMO_POINTS + 250
    assert second["balance"] == INITIAL_DEMO_POINTS + 250
    assert second["idempotent_replay"] is True
    assert len(entries) == 1


def test_reset_sets_balance_to_default(client):
    client.post("/api/demo/wallet/add-points", json={"amount": 250, "reason": "デモポイント追加"})
    response = client.post("/api/demo/wallet/reset", json={"reason": "デモ残高リセット"})
    assert response.status_code == 200
    assert response.json()["balance"] == INITIAL_DEMO_POINTS


def test_reset_creates_ledger_and_audit_event(client, db_conn):
    client.post("/api/demo/wallet/add-points", json={"amount": 250, "reason": "デモポイント追加"})
    client.post("/api/demo/wallet/reset", json={"reason": "デモ残高リセット", "idempotency_key": "reset-key-1"})
    entry = list_ledger(db_conn)[0]
    event = list_audit_events(db_conn)[0]
    assert entry["entry_type"] == "demo_balance_reset"
    assert entry["balance_before"] == INITIAL_DEMO_POINTS + 250
    assert entry["balance_after"] == INITIAL_DEMO_POINTS
    assert entry["idempotency_key"] == "reset-key-1"
    assert event["event_type"] == "demo_balance_reset_created"


def test_demo_predict_with_idempotency_key_does_not_double_deduct(client, db_conn, sample_markets):
    payload = {
        "market_id": sample_markets[0]["market_id"],
        "outcome": "YES",
        "stake": 100,
        "idempotency_key": "predict-key-1",
    }
    first = client.post("/api/demo/predict", json=payload).json()
    second = client.post("/api/demo/predict", json=payload).json()
    prediction_entries = [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "prediction"]
    assert first["balance"] == INITIAL_DEMO_POINTS - 100
    assert second["balance"] == INITIAL_DEMO_POINTS - 100
    assert second["idempotent_replay"] is True
    assert len(prediction_entries) == 1


def test_demo_predict_without_idempotency_key_preserves_current_behavior(client, db_conn, sample_markets):
    payload = {"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 100}
    client.post("/api/demo/predict", json=payload)
    client.post("/api/demo/predict", json=payload)
    prediction_entries = [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "prediction"]
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS - 200
    assert len(prediction_entries) == 2


def test_settlement_ledger_includes_reference_metadata(client, db_conn, sample_markets):
    response = client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 100},
    )
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])
    client.post("/api/demo/settle")
    settlement_entry = [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "settlement_win"][0]
    assert settlement_entry["balance_before"] == response.json()["balance"]
    assert settlement_entry["reference_type"] == "demo_settlement"
    assert settlement_entry["reference_id"]


def test_demo_wallet_page_renders(client):
    response = client.get("/demo-wallet")
    assert response.status_code == 200
    html = response.text
    assert "マイスコア" in html
    assert "デモポイント調整" in html
    assert "初期状態に戻す" in html
    assert "デモポイント履歴" in html
    assert "非換金" in html
