import re
from dataclasses import replace
from pathlib import Path
from html.parser import HTMLParser

from app.storage import (
    INITIAL_DEMO_POINTS,
    get_balance,
    get_ledger_entry,
    get_settlement_by_position_id,
    insert_audit_event,
    insert_realtime_update,
    list_ledger,
    verify_audit_chain,
)
from app.storage import replace_markets


class TextOnlyParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts = []

    def handle_data(self, data):
        self.parts.append(data)


def visible_html(html: str) -> str:
    return re.sub(r"<script\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)


def visible_text(html: str) -> str:
    parser = TextOnlyParser()
    parser.feed(visible_html(html))
    return " ".join(parser.parts)


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
    assert payload["markets"][0]["realtime_status"] == "rest_only"
    assert "best_bid" in payload["markets"][0]
    assert "best_ask" in payload["markets"][0]
    assert "last_trade_price" in payload["markets"][0]


def test_api_realtime_status_renders_json(client):
    response = client.get("/api/realtime/status")
    assert response.status_code == 200
    payload = response.json()
    expected = {
        "ws_enabled",
        "ws_top_n",
        "ws_stale_seconds",
        "latest_update_at",
        "update_count",
        "live_market_update_count",
        "stale_market_update_count",
        "rest_only_count",
    }
    assert expected.issubset(payload.keys())
    assert payload["rest_only_count"] >= 1


def test_api_markets_include_all_returns_hidden_markets(client, db_conn, sample_markets):
    closed = dict(sample_markets[0])
    closed["market_id"] = "route-closed-market"
    closed["closed"] = True
    replace_markets(db_conn, [sample_markets[0], closed])
    default_response = client.get("/api/markets").json()
    include_all_response = client.get(
        "/api/markets?include_all=true",
        headers={"x-demo-admin-token": "test-admin"},
    ).json()
    assert default_response["count"] == 1
    assert include_all_response["count"] == 2
    assert include_all_response["hidden_closed_count"] == 1


def test_strict_participant_mode_rejects_unknown_code(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, strict_participant_access=True, participant_codes="collab-1"),
    )

    response = client.post("/demo-user", data={"demo_user": "unknown"})

    assert response.status_code == 403


def test_strict_participant_mode_accepts_configured_code_and_sets_cookie(client, monkeypatch):
    import app.main as main

    monkeypatch.setattr(
        main,
        "settings",
        replace(main.settings, strict_participant_access=True, participant_codes="collab-1"),
    )

    response = client.post("/demo-user", data={"demo_user": "collab-1"})

    assert response.status_code == 303
    assert response.cookies.get("demo_user_id") == "collab-1"
    assert "httponly" in response.headers["set-cookie"].lower()


def test_non_strict_participant_override_remains_compatible(client):
    response = client.get("/api/demo/balance", headers={"x-demo-user": "ad-hoc-collab"})

    assert response.status_code == 200
    assert response.json()["user_id"] == "ad-hoc-collab"
    assert response.json()["balance"] == INITIAL_DEMO_POINTS


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


def test_api_demo_settle_returns_summary(client, db_conn, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 20},
    )
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])

    response = client.post("/api/demo/settle")

    assert response.status_code == 200
    payload = response.json()
    assert payload["checked_count"] == 1
    assert payload["settled_win_count"] == 1
    assert payload["settled_loss_count"] == 0
    assert payload["total_payout"] > 0
    assert "ws_candidate_count" in payload
    assert "ws_confirmed_count" in payload
    assert "ws_unconfirmed_count" in payload
    assert "ws_conflict_count" in payload
    assert "rest_only_settled_count" in payload


def test_api_demo_resolution_candidates_returns_summary(client, db_conn, sample_markets):
    insert_realtime_update(
        db_conn,
        {
            "market_id": sample_markets[0]["market_id"],
            "asset_id": "asset-yes",
            "event_type": "market_resolved",
            "winning_outcome": "YES",
            "raw_event_json": "{}",
        },
    )
    db_conn.commit()
    response = client.get("/api/demo/resolution-candidates", headers={"x-demo-admin-token": "test-admin"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["candidate_count"] == 1
    assert payload["markets_with_candidates_count"] == 1
    assert payload["candidates"][0]["winning_outcome"] == "YES"


def test_api_demo_results_includes_updated_status_after_settlement(client, db_conn, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "NO", "stake": 20},
    )
    resolved = dict(sample_markets[0])
    resolved["closed"] = True
    resolved["active"] = False
    resolved["probabilities"] = {"YES": 1.0, "NO": 0.0}
    replace_markets(db_conn, [resolved])
    client.post("/api/demo/settle")

    payload = client.get("/api/demo/results").json()

    assert payload["results"][0]["status"] == "settled_loss"
    assert payload["results"][0]["status_label"] == "不的中"
    assert payload["results"][0]["rest_confirmation_label"] == "参考データ判定"
    assert payload["settled_count"] == 1


def test_debug_source_status_returns_expected_keys(client):
    response = client.get("/api/debug/source-status", headers={"x-demo-admin-token": "test-admin"})
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
    assert "市場データ" in html
    assert "表示中" in html
    assert "取得合計" in html
    assert "非表示" in html
    assert "マーケット一覧へ戻る" in html


def test_view_all_markets_link_targets_product_ui_not_api(client):
    html = client.get("/").text
    assert "全マーケットを見る" in html
    assert 'href="/?lang=ja#market-list"' in html
    assert ">全マーケットを見る</a>" in html
    assert 'href="/api/markets' not in visible_html(html)


def test_market_list_page_is_product_ui_not_raw_json(client):
    response = client.get("/")
    text = visible_text(response.text)
    assert response.headers["content-type"].startswith("text/html")
    assert "マーケット" in text
    assert "デモ参加" in text
    assert '"markets":' not in response.text
    assert '{"' not in visible_html(response.text)


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
    assert "現在のマイスコア" in html
    assert "Join demo" not in visible_text(html)
    assert "Demo participation" not in visible_text(html)


def test_market_detail_page_is_product_ui_not_raw_json(client):
    response = client.get("/markets/sample-market-tokyo-rain")
    assert response.headers["content-type"].startswith("text/html")
    assert "予想する" in visible_text(response.text)
    assert '"market_id":' not in response.text
    assert '{"' not in visible_html(response.text)


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


def test_wallet_ledger_consistency_after_vote(client, db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    response = client.post(
        "/api/demo/predict",
        json={"market_id": market_id, "outcome": "YES", "stake": 75},
    )

    assert response.status_code == 200
    assert response.json()["balance"] == INITIAL_DEMO_POINTS - 75
    ledger = list_ledger(db_conn)
    prediction_entries = [entry for entry in ledger if entry["entry_type"] == "prediction"]
    assert len(prediction_entries) == 1
    assert prediction_entries[0]["balance_before"] == INITIAL_DEMO_POINTS
    assert prediction_entries[0]["balance_after"] == INITIAL_DEMO_POINTS - 75
    wallet = client.get("/demo-wallet").text
    assert "デモ参加利用" in wallet
    assert "75.00" in wallet


def test_new_audit_events_receive_event_hash(db_conn):
    event = insert_audit_event(
        db_conn,
        event_type="test_event",
        user_id="participant-1",
        route="/test",
        after={"ok": True},
        note="test audit event",
    )

    assert event["event_hash"]
    assert event["previous_event_hash"] == ""
    assert event["integrity_payload_json"]


def test_verify_audit_chain_returns_verified_for_untampered_events(db_conn):
    insert_audit_event(db_conn, event_type="first", user_id="participant-1", after={"step": 1})
    insert_audit_event(db_conn, event_type="second", user_id="participant-1", after={"step": 2})

    result = verify_audit_chain(db_conn)

    assert result["integrity_status"] == "verified"
    assert result["checked_count"] == 2
    assert result["verified_count"] == 2
    assert result["broken_count"] == 0


def test_verify_audit_chain_returns_broken_after_audit_row_tamper(db_conn):
    event = insert_audit_event(db_conn, event_type="tamper_test", user_id="participant-1", after={"ok": True})
    db_conn.execute("update demo_audit_events set after_json = ? where id = ?", ('{"ok":false}', event["id"]))
    db_conn.commit()

    result = verify_audit_chain(db_conn)

    assert result["integrity_status"] == "broken"
    assert result["broken_count"] == 1
    assert result["first_broken_event_id"] == event["id"]


def test_admin_audit_integrity_rejects_unauthenticated_access(client):
    response = client.get("/api/admin/audit/integrity")

    assert response.status_code == 403


def test_admin_audit_integrity_returns_status_with_admin_token(client, db_conn):
    insert_audit_event(db_conn, event_type="integrity_api_test", user_id="participant-1")

    response = client.get("/api/admin/audit/integrity", headers={"x-demo-admin-token": "test-admin"})

    assert response.status_code == 200
    assert response.json()["integrity_status"] == "verified"


def test_ledger_reversal_creates_opposite_row_and_updates_balance(client, db_conn):
    original = client.post(
        "/api/demo/wallet/add-points",
        json={"amount": 125, "reason": "local demo adjustment"},
    ).json()["ledger_entry"]

    response = client.post(
        "/api/demo/ledger/reversal",
        json={"ledger_entry_id": original["id"], "reason": "entered for wrong participant"},
    )

    assert response.status_code == 200
    payload = response.json()
    reversal = payload["ledger_entry"]
    assert reversal["entry_type"] == "correction_reversal"
    assert reversal["amount"] == -original["amount"]
    assert reversal["reference_id"] == str(original["id"])
    assert payload["balance"] == INITIAL_DEMO_POINTS
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS


def test_ledger_reversal_does_not_mutate_original_row(client, db_conn):
    original = client.post(
        "/api/demo/wallet/add-points",
        json={"amount": 75, "reason": "local demo adjustment"},
    ).json()["ledger_entry"]
    before = get_ledger_entry(db_conn, original["id"])

    client.post(
        "/api/demo/ledger/reversal",
        json={"ledger_entry_id": original["id"], "reason": "entered for wrong participant"},
    )

    assert get_ledger_entry(db_conn, original["id"]) == before


def test_ledger_reversal_idempotency_replay_does_not_double_apply(client, db_conn):
    original = client.post(
        "/api/demo/wallet/add-points",
        json={"amount": 50, "reason": "local demo adjustment"},
    ).json()["ledger_entry"]

    first = client.post(
        "/api/demo/ledger/reversal",
        json={"ledger_entry_id": original["id"], "reason": "duplicate form submit", "idempotency_key": "rev-1"},
    )
    second = client.post(
        "/api/demo/ledger/reversal",
        json={"ledger_entry_id": original["id"], "reason": "duplicate form submit", "idempotency_key": "rev-1"},
    )

    assert first.status_code == 200
    assert second.status_code == 200
    assert second.json()["idempotent_replay"] is True
    assert second.json()["ledger_entry"]["id"] == first.json()["ledger_entry"]["id"]
    assert get_balance(db_conn) == INITIAL_DEMO_POINTS
    reversal_rows = [entry for entry in list_ledger(db_conn) if entry["entry_type"] == "correction_reversal"]
    assert len(reversal_rows) == 1


def test_reversing_reversal_entry_is_rejected(client):
    original = client.post(
        "/api/demo/wallet/add-points",
        json={"amount": 25, "reason": "local demo adjustment"},
    ).json()["ledger_entry"]
    reversal = client.post(
        "/api/demo/ledger/reversal",
        json={"ledger_entry_id": original["id"], "reason": "entered for wrong participant"},
    ).json()["ledger_entry"]

    response = client.post(
        "/api/demo/ledger/reversal",
        json={"ledger_entry_id": reversal["id"], "reason": "not allowed"},
    )

    assert response.status_code == 400


def test_demo_results_page_renders(client, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 20},
    )
    response = client.get("/demo-results")
    assert response.status_code == 200
    html = response.text
    assert "結果確認" in html
    assert "結果を確認する" in html
    assert "結果待ち" in html
    assert "参加デモポイント" in html
    assert "確定理由" in html
    assert "参照元" in html
    assert "明確な結果をまだ確認できていません。" in html
    assert "<th>Market</th>" not in html
    assert "<th>Outcome</th>" not in html
    assert "pending" not in visible_html(html)
    assert "推定デモリターン" not in html


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
    assert "マイスコア" in combined


def test_app_ui_action_text_avoids_forbidden_labels(client):
    combined = client.get("/").text + client.get("/markets/sample-market-tokyo-rain").text
    assert "賭ける" not in combined
    assert "ベット" not in combined
    assert "Bet now" not in combined
    assert "place bet" not in combined
    assert ">buy<" not in combined
    assert ">sell<" not in combined


def test_rendered_ui_avoids_forbidden_demo_wallet_words(client):
    combined = visible_html(
        client.get("/").text
        + client.get("/markets/sample-market-tokyo-rain").text
        + client.get("/demo-wallet").text
        + client.get("/demo-positions").text
        + client.get("/demo-results").text
    )
    for term in [
        "入金",
        "出金",
        "賭ける",
        "ベット",
        "購入",
        "売却",
        "利益確定",
        "稼ぐ",
        "儲かる",
        "deposit",
        "withdraw",
        "cashout",
        "buy",
        "sell",
        "profit",
        "earn money",
        "real money",
        "investment",
        "gambling",
        "cash out",
        "Bet now",
        "place bet",
    ]:
        assert term not in combined


def test_public_pages_do_not_link_directly_to_include_all_api(client):
    combined = (
        client.get("/").text
        + client.get("/markets/sample-market-tokyo-rain").text
        + client.get("/demo-wallet").text
        + client.get("/demo-positions").text
        + client.get("/demo-results").text
    )
    assert "/api/markets?include_all=true" not in combined


def test_public_pages_do_not_link_to_protected_audit_review(client):
    combined = (
        client.get("/").text
        + client.get("/markets/sample-market-tokyo-rain").text
        + client.get("/demo-wallet").text
        + client.get("/demo-positions").text
        + client.get("/demo-results").text
    )
    assert 'href="/admin/audit' not in combined
    assert 'href="/admin/audit.csv' not in combined


def test_public_pages_avoid_developer_realtime_and_finance_labels(client):
    raw_html = (
        client.get("/").text
        + client.get("/markets/sample-market-tokyo-rain").text
        + client.get("/demo-wallet").text
        + client.get("/demo-positions").text
        + client.get("/demo-results").text
    )
    combined = visible_html(raw_html)
    neutralized_market_terms = [
        "最良" + "買い" + "気配",
        "最良" + "売り" + "気配",
        "買い" + "気配",
        "売り" + "気配",
    ]
    for term in [
        "REST",
        "WebSocket",
        "stale",
        "fallback",
        "polling",
        "fresh" + "ness",
        "API state",
        "debug",
        "raw",
        "stream",
        "internal",
        "Near-real-time",
        "30秒ごと",
        "処理ID",
        "参照ID",
        "Stake",
        "payout",
        "<th>Market</th>",
        "<th>Outcome</th>",
        "推定デモリターン",
        "pending",
        "Demo Point Management",
        "デモポイント管理",
        *neutralized_market_terms,
    ]:
        assert term not in combined
    assert not re.search(r"(?<![A-Za-z0-9])WS(?![A-Za-z0-9])", visible_text(raw_html))


def test_refresh_copy_is_user_friendly_and_interval_hidden(client):
    html = client.get("/").text
    text = visible_text(html)
    assert "市場データ" in text
    assert "ライブ" in text
    assert "最終更新" in text
    assert "表示中に静かに更新します" not in text
    assert "参考データの更新確認" not in text
    assert "リアルタイム状態" not in text
    assert "30秒" not in text
    assert "poll" not in text.lower()
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert 'fetch("/api/markets")' in script
    assert 'fetch("/api/markets?include_all=true")' not in script
    assert "pollVisibleMarket" in script


def test_result_transparency_text_is_product_safe(client, sample_markets):
    client.post(
        "/api/demo/predict",
        json={"market_id": sample_markets[0]["market_id"], "outcome": "YES", "stake": 20},
    )
    html = client.get("/demo-results").text
    text = visible_text(html)
    assert "明確な結果が確認できる場合だけ、参考スコアへ反映します。" in text
    assert "settlement" not in text.lower()
    assert "payout" not in text.lower()


def test_commercial_acceptance_audit_document_exists():
    audit = Path("docs/commercial_product_acceptance_audit.md").read_text(encoding="utf-8")
    for phrase in [
        "route coverage",
        "Normal navigation lands on rendered product pages",
        "Japanese is the default core flow",
        "Protected audit review remains unlinked",
    ]:
        assert phrase in audit


def test_demo_wallet_page_shows_non_cashable_non_transferable_non_exchangeable_notice(client):
    html = client.get("/demo-wallet").text
    assert "マイスコア" in html
    assert "非換金" in html
    assert "換金" in html
    assert "譲渡" in html
    assert "商品・ギフト券・Pay・株引換券・暗号資産" in html
    assert "交換はできません" in html


def test_last_updated_rendering_is_not_raw_iso(client):
    html = client.get("/").text
    assert "T" not in html.split('id="latest-update">', 1)[1].split("</strong>", 1)[0]
    assert "+00:00" not in html
    assert "最終更新" in html


def test_market_cards_hide_internal_transport_status(client):
    html = client.get("/").text
    text = visible_text(html)
    assert "デモ参加可" in text
    assert "外部の公開データを参照しています" in text
    for phrase in [
        "外部予測市場の公開参考データ",
        "RESTのみ",
        "WebSocket更新中",
        "WebSocket最終更新",
        "リアルタイム状態",
    ]:
        assert phrase not in text
