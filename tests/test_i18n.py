from app.i18n import (
    DEFAULT_LANG,
    confirmation_status_label,
    normalize_lang,
    realtime_status_label,
    status_label,
    t,
)


def test_normalize_lang():
    assert normalize_lang("ja") == "ja"
    assert normalize_lang("ja-JP") == "ja"
    assert normalize_lang("en") == "en"
    assert normalize_lang("en-US") == "en"
    assert normalize_lang(None) == DEFAULT_LANG
    assert normalize_lang("fr") == DEFAULT_LANG
    assert normalize_lang("") == DEFAULT_LANG


def test_translation_ja_en():
    assert t("ja", "wallet.add_points") == "デモポイント追加"
    assert t("en", "wallet.add_points") == "Add demo points"
    assert t("ja", "result.check") == "結果を確認する"
    assert t("en", "result.check") == "Check results"
    assert t("ja", "nav.demo_wallet") == "デモポイント管理"
    assert t("en", "nav.demo_wallet") == "Demo Point Management"


def test_missing_translation_returns_key_or_default():
    assert t("ja", "missing.key") == "missing.key"
    assert t("en", "missing.key") == "missing.key"
    assert t("ja", "missing.key", "fallback") == "fallback"
    assert t("en", "missing.key", "fallback") == "fallback"


def test_status_labels():
    assert status_label("ja", "settled_win") == "的中"
    assert status_label("en", "settled_win") == "Won"

    assert status_label("ja", "settled_loss") == "不的中"
    assert status_label("en", "settled_loss") == "Lost"

    assert status_label("ja", "settlement_pending") == "判定保留"
    assert status_label("en", "settlement_pending") == "Pending confirmation"

    assert status_label("ja", "settlement_unknown") == "判定不明"
    assert status_label("en", "settlement_unknown") == "Unknown"

    assert status_label("ja", None) == "なし"
    assert status_label("en", None) == "None"


def test_confirmation_status_labels():
    assert confirmation_status_label("ja", "confirmed_match") == "REST確認済み"
    assert confirmation_status_label("en", "confirmed_match") == "REST confirmed"

    assert confirmation_status_label("ja", "rest_clear_without_candidate") == "REST判定"
    assert confirmation_status_label("en", "rest_clear_without_candidate") == "REST decision"

    assert confirmation_status_label("ja", "candidate_only_unconfirmed") == "WSのみ未確認"
    assert confirmation_status_label("en", "candidate_only_unconfirmed") == "WS only, unconfirmed"

    assert confirmation_status_label("ja", "conflict") == "WS/REST不一致"
    assert confirmation_status_label("en", "conflict") == "WS/REST conflict"

    assert confirmation_status_label("ja", "unclear") == "判定不明"
    assert confirmation_status_label("en", "unclear") == "Unknown"


def test_realtime_status_labels():
    assert realtime_status_label("ja", "rest_only") == "RESTのみ"
    assert realtime_status_label("en", "rest_only") == "REST only"

    assert realtime_status_label("ja", "ws_live") == "WebSocket更新中"
    assert realtime_status_label("en", "ws_live") == "WebSocket live"

    assert realtime_status_label("ja", "ws_stale") == "WebSocket stale"
    assert realtime_status_label("en", "ws_stale") == "WebSocket stale"

    assert realtime_status_label("ja", None) == "RESTのみ"
    assert realtime_status_label("en", None) == "REST only"
