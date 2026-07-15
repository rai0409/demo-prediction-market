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
    assert t("ja", "wallet.add_points") == "デモポイント調整"
    assert t("en", "wallet.add_points") == "Demo point adjustment"
    assert t("ja", "result.check") == "結果を確認する"
    assert t("en", "result.check") == "Check results"
    assert t("ja", "nav.demo_wallet") == "マイスコア"
    assert t("en", "nav.demo_wallet") == "My Score"
    assert t("ja", "market.data") == "市場データ"
    assert t("en", "market.data") == "Market data"
    assert t("ja", "market.live") == "ライブ"
    assert t("en", "market.live") == "Live"
    assert t("ja", "market.updated") == "最終更新"
    assert t("en", "market.updated") == "Updated"
    assert t("ja", "market.catalog_title") == "全マーケット"
    assert t("en", "market.catalog_title") == "All markets"
    assert t("ja", "pagination.previous") == "前へ"
    assert t("en", "pagination.next") == "Next"


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
    assert confirmation_status_label("ja", "confirmed_match") == "参考データ確認済み"
    assert confirmation_status_label("en", "confirmed_match") == "Reference data confirmed"

    assert confirmation_status_label("ja", "rest_clear_without_candidate") == "参考データ判定"
    assert confirmation_status_label("en", "rest_clear_without_candidate") == "Reference data decision"

    assert confirmation_status_label("ja", "candidate_only_unconfirmed") == "結果候補のみ"
    assert confirmation_status_label("en", "candidate_only_unconfirmed") == "Candidate only"

    assert confirmation_status_label("ja", "conflict") == "参考データ不一致"
    assert confirmation_status_label("en", "conflict") == "Reference data mismatch"

    assert confirmation_status_label("ja", "unclear") == "判定不明"
    assert confirmation_status_label("en", "unclear") == "Unknown"


def test_realtime_status_labels():
    assert realtime_status_label("ja", "rest_only") == "参考データ"
    assert realtime_status_label("en", "rest_only") == "Reference data"

    assert realtime_status_label("ja", "ws_live") == "最新情報を自動更新"
    assert realtime_status_label("en", "ws_live") == "Auto-updating"

    assert realtime_status_label("ja", "ws_stale") == "更新確認中"
    assert realtime_status_label("en", "ws_stale") == "Checking updates"

    assert realtime_status_label("ja", None) == "参考データ"
    assert realtime_status_label("en", None) == "Reference data"
