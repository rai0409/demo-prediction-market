from app.storage import INITIAL_DEMO_POINTS, get_balance, list_ledger, list_positions


def _predict(client, user_id, market_id, stake):
    return client.post(
        "/api/demo/predict",
        headers={"x-demo-user": user_id},
        json={"market_id": market_id, "outcome": "YES", "stake": stake},
    )


def test_demo_api_balances_positions_and_ledger_are_user_scoped(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]

    alice_response = _predict(client, "alice", market_id, 100)
    bob_response = _predict(client, "bob", market_id, 250)

    assert alice_response.status_code == 200
    assert bob_response.status_code == 200
    assert alice_response.json()["balance"] == INITIAL_DEMO_POINTS - 100
    assert bob_response.json()["balance"] == INITIAL_DEMO_POINTS - 250

    alice_positions = client.get("/api/demo/positions", headers={"x-demo-user": "alice"}).json()
    bob_positions = client.get("/api/demo/positions", headers={"x-demo-user": "bob"}).json()

    assert alice_positions["user_id"] == "alice"
    assert bob_positions["user_id"] == "bob"
    assert [position["stake"] for position in alice_positions["positions"]] == [100]
    assert [position["stake"] for position in bob_positions["positions"]] == [250]
    assert [entry["amount"] for entry in alice_positions["ledger"] if entry["entry_type"] == "prediction"] == [-100]
    assert [entry["amount"] for entry in bob_positions["ledger"] if entry["entry_type"] == "prediction"] == [-250]

    assert get_balance(db_conn, "alice") == INITIAL_DEMO_POINTS - 100
    assert get_balance(db_conn, "bob") == INITIAL_DEMO_POINTS - 250
    assert len(list_positions(db_conn, "alice")) == 1
    assert len(list_positions(db_conn, "bob")) == 1
    assert len([entry for entry in list_ledger(db_conn, "alice") if entry["entry_type"] == "prediction"]) == 1
    assert len([entry for entry in list_ledger(db_conn, "bob") if entry["entry_type"] == "prediction"]) == 1


def test_demo_settlement_only_updates_current_user(client, sample_markets):
    market_id = sample_markets[0]["market_id"]
    _predict(client, "alice", market_id, 100)
    _predict(client, "bob", market_id, 200)

    alice_settlement = client.post("/api/demo/settle", headers={"x-demo-user": "alice"}).json()
    alice_results = client.get("/api/demo/results", headers={"x-demo-user": "alice"}).json()
    bob_results = client.get("/api/demo/results", headers={"x-demo-user": "bob"}).json()

    assert alice_settlement["checked_count"] == 1
    assert alice_results["user_id"] == "alice"
    assert bob_results["user_id"] == "bob"
    assert len(alice_results["results"]) == 1
    assert len(bob_results["results"]) == 1
    assert alice_results["results"][0]["stake"] == 100
    assert bob_results["results"][0]["stake"] == 200


def test_demo_user_query_does_not_override_identity(client):
    baseline = client.get("/api/demo/balance")
    response = client.get("/?demo_user=limited-user")
    queried = client.get("/api/demo/balance?demo_user=limited-user")

    assert baseline.status_code == 200
    assert response.status_code == 200
    assert queried.status_code == 200

    # Query parameters are no longer an accepted identity source.
    assert "demo_user_id=limited-user" not in response.headers.get(
        "set-cookie",
        "",
    )
    assert queried.json()["user_id"] == baseline.json()["user_id"]


