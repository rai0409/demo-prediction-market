from dataclasses import replace


def test_normal_japanese_ui_has_no_participant_switch(client):
    response = client.get("/?lang=ja")

    assert response.status_code == 200
    assert "demo-user-switch" not in response.text
    assert "demo-user-input" not in response.text
    assert 'action="/demo-user' not in response.text


def test_normal_english_ui_has_no_participant_switch(client):
    response = client.get("/?lang=en")

    assert response.status_code == 200
    assert "demo-user-switch" not in response.text
    assert "demo-user-input" not in response.text
    assert 'action="/demo-user' not in response.text


def test_demo_user_route_is_disabled_when_switch_setting_is_false(
    client,
    monkeypatch,
):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            participant_switch_enabled=False,
        ),
    )

    response = client.post(
        "/demo-user",
        data={"demo_user": "other-participant"},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert "demo_user_id" not in response.cookies


def test_demo_user_query_parameter_does_not_override_identity(
    client,
    monkeypatch,
):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            strict_participant_access=False,
            participant_switch_enabled=False,
            allow_demo_user_header=False,
        ),
    )

    baseline = client.get("/api/demo/balance")
    queried = client.get(
        "/api/demo/balance?demo_user=other-participant"
    )

    assert baseline.status_code == 200
    assert queried.status_code == 200
    assert queried.json()["user_id"] == baseline.json()["user_id"]


def test_demo_user_header_is_ignored_when_setting_is_false(
    client,
    monkeypatch,
):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            strict_participant_access=False,
            participant_switch_enabled=False,
            allow_demo_user_header=False,
        ),
    )

    baseline = client.get("/api/demo/balance")
    spoofed = client.get(
        "/api/demo/balance",
        headers={"x-demo-user": "spoofed-participant"},
    )

    assert baseline.status_code == 200
    assert spoofed.status_code == 200
    assert spoofed.json()["user_id"] == baseline.json()["user_id"]
    assert "demo_user_id=spoofed-participant" not in spoofed.headers.get(
        "set-cookie",
        "",
    )


def test_demo_user_header_can_be_enabled_for_internal_tests(
    client,
    monkeypatch,
):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            strict_participant_access=False,
            allow_demo_user_header=True,
        ),
    )

    response = client.get(
        "/api/demo/balance",
        headers={"x-demo-user": "internal-participant"},
    )

    assert response.status_code == 200
    assert response.json()["user_id"] == "internal-participant"


def test_switch_route_accepts_only_allowed_code_when_explicitly_enabled(
    client,
    monkeypatch,
):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            strict_participant_access=True,
            participant_codes="allowed-participant",
            participant_switch_enabled=True,
            allow_demo_user_header=False,
        ),
    )

    accepted = client.post(
        "/demo-user",
        data={"demo_user": "allowed-participant"},
        follow_redirects=False,
    )
    rejected = client.post(
        "/demo-user",
        data={"demo_user": "not-allowed"},
        follow_redirects=False,
    )

    assert accepted.status_code == 303
    assert accepted.cookies.get(
        main.demo_session_cookie_name()
    ) == "allowed-participant"
    assert rejected.status_code == 403


def test_rejection_log_does_not_trust_header_when_disabled(
    client,
    monkeypatch,
):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(
            main.settings,
            participant_switch_enabled=False,
            allow_demo_user_header=False,
        ),
    )
    main._operation_rejections.clear()

    response = client.post(
        "/demo-user",
        headers={"x-demo-user": "forged-participant"},
        data={"demo_user": "other"},
        follow_redirects=False,
    )

    assert response.status_code == 404
    assert main._operation_rejections
    assert (
        main._operation_rejections[-1]["参加者"]
        != "forged-participant"
    )
