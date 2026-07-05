from __future__ import annotations

from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

SUPPORTED_LANGS = {"ja", "en"}
DEFAULT_LANG = "ja"
LANG_COOKIE_NAME = "demo_lang"


TRANSLATIONS: dict[str, dict[str, str]] = {
    "ja": {
        # App / navigation
        "app.title": "Demo Prediction Market Viewer",
        "app.subtitle": "外部予測市場の公開参考データを使うローカル・デモ予想アプリ",
        "nav.markets": "マーケット",
        "nav.demo_wallet": "マイスコア",
        "nav.positions": "デモポジション",
        "nav.results": "結果確認",
        "nav.api_docs": "API Docs",
        "nav.language": "言語",

        # Common
        "common.status": "状態",
        "common.created_at": "作成日時",
        "common.updated_at": "更新日時",
        "common.request_id": "内部記録",
        "common.reference_id": "内部記録",
        "common.error": "エラー",
        "common.success": "成功",
        "common.loading": "読み込み中",
        "common.none": "なし",
        "common.yes": "はい",
        "common.no": "いいえ",
        "common.original": "原文",
        "common.reason": "理由",
        "common.amount": "数量",
        "common.note": "メモ",

        # Market
        "market.title": "マーケット",
        "market.data_status": "データ状態",
        "market.volume": "出来高",
        "market.volume_24h": "24時間出来高",
        "market.liquidity": "流動性",
        "market.end_date": "終了予定",
        "market.outcomes": "選択肢",
        "market.original_text": "原文",
        "market.description": "説明",
        "market.resolution": "判定条件",
        "market.last_fetch": "最終取得",
        "market.source": "データソース",
        "market.displayable": "表示対象",
        "market.hidden_reason": "非表示理由",

        # Demo participation
        "demo.join": "デモ参加",
        "demo.join_action": "予想する",
        "demo.points": "デモポイント",
        "demo.balance": "マイスコア",
        "demo.outcome": "選択肢",
        "demo.amount": "デモポイント数",
        "demo.submit": "デモ参加する",
        "demo.position": "デモポジション",
        "demo.positions": "デモポジション",
        "demo.history": "予想履歴",
        "demo.ledger": "デモポイント履歴",
        "demo.estimated_return": "参考スコア",
        "demo.max_loss": "最大デモポイント減少",
        "demo.success": "デモ参加を記録しました。",
        "demo.error": "デモ参加の記録に失敗しました。",

        # Wallet / demo point management
        "wallet.title": "マイスコア",
        "wallet.balance": "マイスコア",
        "wallet.add_points": "デモポイント調整",
        "wallet.reset": "初期状態に戻す",
        "wallet.history": "デモポイント履歴",
        "wallet.audit": "内部記録",
        "wallet.reason": "理由",
        "wallet.amount": "追加するデモポイント",
        "wallet.add_success": "デモポイントを調整しました。",
        "wallet.reset_success": "初期状態に戻しました。",
        "wallet.note_non_cashable": "デモポイントは商用product基準の検証用非換金スコアです。換金、譲渡、商品・ギフト券・Pay・株引換券・暗号資産との交換はできません。",
        "wallet.summary": "デモポイント概要",
        "wallet.total_added": "追加デモポイント",
        "wallet.total_used": "デモ参加で使用",
        "wallet.total_settled": "デモ精算",
        "wallet.total_adjusted": "調整",
        "wallet.ledger_count": "履歴件数",

        # Results / settlement
        "result.title": "結果確認",
        "result.check": "結果を確認する",
        "result.pending": "判定保留",
        "result.unknown": "判定不明",
        "result.win": "的中",
        "result.loss": "不的中",
        "result.candidates": "結果候補",
        "result.ws_detected": "結果候補",
        "result.rest_confirmation": "参考データ確認",
        "result.rest_confirmed": "参考データ確認済み",
        "result.ws_unconfirmed": "結果候補のみ",
        "result.ws_rest_conflict": "参考データ不一致",
        "result.rest_only": "参考データ判定",
        "result.summary": "確認結果",
        "result.payout_demo_points": "参考スコア",
        "result.settlement_source": "判定ソース",
        "result.settlement_note": "判定メモ",
        "result.settled_at": "結果確定日時",
        "result.current_balance": "現在のデモ残高",
        "result.checked_count": "確認件数",
        "result.win_count": "的中",
        "result.loss_count": "不的中",
        "result.pending_count": "判定保留",
        "result.unknown_count": "判定不明",
        "result.ws_confirmed_count": "確認済み",
        "result.ws_unconfirmed_count": "結果候補のみ",
        "result.ws_conflict_count": "参考データ不一致",
        "result.rest_only_count": "参考データ判定",

        # Realtime / WebSocket
        "realtime.status": "更新状態",
        "realtime.rest_only": "参考データ",
        "realtime.ws_live": "最新情報を自動更新",
        "realtime.ws_stale": "更新確認中",
        "realtime.best_bid": "最良買い気配",
        "realtime.best_ask": "最良売り気配",
        "realtime.last_trade": "直近参考値",
        "realtime.spread": "スプレッド",
        "realtime.last_event": "最終更新",

        # Data source status
        "source.live": "外部参考データ",
        "source.sample_fallback": "参考データ",
        "source.live_failed_sample_fallback": "参考データ",
        "source.unknown": "不明",

        # Internal status display labels
        "status.pending": "結果待ち",
        "status.settlement_pending": "判定保留",
        "status.settlement_unknown": "判定不明",
        "status.settled_win": "的中",
        "status.settled_loss": "不的中",
        "status.simulated": "デモ参加済み",

        # Confirmation display labels
        "confirmation.confirmed_match": "参考データ確認済み",
        "confirmation.rest_clear_without_candidate": "参考データ判定",
        "confirmation.candidate_only_unconfirmed": "結果候補のみ",
        "confirmation.conflict": "参考データ不一致",
        "confirmation.unclear": "判定不明",

        # Safety
        "safety.local_only": "このアプリはローカルのデモポイントのみを扱います。",
        "safety.no_cashable": "デモポイントは換金・外部交換できません。",
        "safety.no_real_orders": "実注文・実ポジション作成は行いません。",
    },
    "en": {
        # App / navigation
        "app.title": "Demo Prediction Market Viewer",
        "app.subtitle": "A local demo app using public reference data from external prediction markets",
        "nav.markets": "Markets",
        "nav.demo_wallet": "My Score",
        "nav.positions": "Demo Positions",
        "nav.results": "Check Results",
        "nav.api_docs": "API Docs",
        "nav.language": "Language",

        # Common
        "common.status": "Status",
        "common.created_at": "Created at",
        "common.updated_at": "Updated at",
        "common.request_id": "Internal record",
        "common.reference_id": "Internal record",
        "common.error": "Error",
        "common.success": "Success",
        "common.loading": "Loading",
        "common.none": "None",
        "common.yes": "Yes",
        "common.no": "No",
        "common.original": "Original",
        "common.reason": "Reason",
        "common.amount": "Amount",
        "common.note": "Note",

        # Market
        "market.title": "Market",
        "market.data_status": "Data status",
        "market.volume": "Volume",
        "market.volume_24h": "24h volume",
        "market.liquidity": "Liquidity",
        "market.end_date": "End date",
        "market.outcomes": "Outcomes",
        "market.original_text": "Original",
        "market.description": "Description",
        "market.resolution": "Resolution criteria",
        "market.last_fetch": "Last fetch",
        "market.source": "Data source",
        "market.displayable": "Displayable",
        "market.hidden_reason": "Hidden reason",

        # Demo participation
        "demo.join": "Demo participation",
        "demo.join_action": "Join demo",
        "demo.points": "Demo points",
        "demo.balance": "My score",
        "demo.outcome": "Outcome",
        "demo.amount": "Demo point amount",
        "demo.submit": "Join demo",
        "demo.position": "Demo position",
        "demo.positions": "Demo positions",
        "demo.history": "Prediction history",
        "demo.ledger": "Demo point history",
        "demo.estimated_return": "Reference score",
        "demo.max_loss": "Maximum demo point decrease",
        "demo.success": "Demo participation was recorded.",
        "demo.error": "Failed to record demo participation.",

        # Wallet / demo point management
        "wallet.title": "My Score",
        "wallet.balance": "My score",
        "wallet.add_points": "Demo point adjustment",
        "wallet.reset": "Restore initial score",
        "wallet.history": "Demo point history",
        "wallet.audit": "Internal records",
        "wallet.reason": "Reason",
        "wallet.amount": "Demo points to add",
        "wallet.add_success": "Demo points were adjusted.",
        "wallet.reset_success": "Initial score was restored.",
        "wallet.note_non_cashable": "Demo points are non-cash validation scores. They cannot be cashed out, transferred, or exchanged for products, gift cards, Pay balances, stock vouchers, crypto assets, or anything outside the app.",
        "wallet.summary": "Demo point summary",
        "wallet.total_added": "Total demo points added",
        "wallet.total_used": "Used for demo participation",
        "wallet.total_settled": "Demo settlement",
        "wallet.total_adjusted": "Adjustments",
        "wallet.ledger_count": "Ledger entries",

        # Results / settlement
        "result.title": "Check Results",
        "result.check": "Check results",
        "result.pending": "Pending confirmation",
        "result.unknown": "Unknown",
        "result.win": "Won",
        "result.loss": "Lost",
        "result.candidates": "Result candidates",
        "result.ws_detected": "Result candidate",
        "result.rest_confirmation": "Reference data check",
        "result.rest_confirmed": "Reference data confirmed",
        "result.ws_unconfirmed": "Candidate only",
        "result.ws_rest_conflict": "Reference data mismatch",
        "result.rest_only": "Reference data decision",
        "result.summary": "Result summary",
        "result.payout_demo_points": "Reference score",
        "result.settlement_source": "Decision source",
        "result.settlement_note": "Decision note",
        "result.settled_at": "Settled at",
        "result.current_balance": "Current demo balance",
        "result.checked_count": "Checked",
        "result.win_count": "Won",
        "result.loss_count": "Lost",
        "result.pending_count": "Pending",
        "result.unknown_count": "Unknown",
        "result.ws_confirmed_count": "Confirmed",
        "result.ws_unconfirmed_count": "Candidate only",
        "result.ws_conflict_count": "Reference data mismatch",
        "result.rest_only_count": "Reference data decision",

        # Realtime / WebSocket
        "realtime.status": "Update status",
        "realtime.rest_only": "Reference data",
        "realtime.ws_live": "Auto-updating",
        "realtime.ws_stale": "Checking updates",
        "realtime.best_bid": "Best bid",
        "realtime.best_ask": "Best ask",
        "realtime.last_trade": "Latest reference value",
        "realtime.spread": "Spread",
        "realtime.last_event": "Last update",

        # Data source status
        "source.live": "External reference data",
        "source.sample_fallback": "Reference data",
        "source.live_failed_sample_fallback": "Reference data",
        "source.unknown": "Unknown",

        # Internal status display labels
        "status.pending": "Pending result",
        "status.settlement_pending": "Pending confirmation",
        "status.settlement_unknown": "Unknown",
        "status.settled_win": "Won",
        "status.settled_loss": "Lost",
        "status.simulated": "Demo participation recorded",

        # Confirmation display labels
        "confirmation.confirmed_match": "Reference data confirmed",
        "confirmation.rest_clear_without_candidate": "Reference data decision",
        "confirmation.candidate_only_unconfirmed": "Candidate only",
        "confirmation.conflict": "Reference data mismatch",
        "confirmation.unclear": "Unknown",

        # Safety
        "safety.local_only": "This app uses local demo points only.",
        "safety.no_cashable": "Demo points cannot be exchanged or transferred outside the app.",
        "safety.no_real_orders": "No real orders or real positions are created.",
    },
}


def normalize_lang(value: str | None) -> str:
    if not value:
        return DEFAULT_LANG

    normalized = value.strip().lower()

    if normalized.startswith("ja"):
        return "ja"
    if normalized.startswith("en"):
        return "en"

    return DEFAULT_LANG


def _get_query_lang(request) -> str | None:
    try:
        value = request.query_params.get("lang")
    except Exception:
        return None
    return value if value in SUPPORTED_LANGS else None


def _get_cookie_lang(request) -> str | None:
    try:
        value = request.cookies.get(LANG_COOKIE_NAME)
    except Exception:
        return None
    return value if value in SUPPORTED_LANGS else None


def _get_accept_language(request) -> str | None:
    try:
        header = request.headers.get("accept-language", "")
    except Exception:
        return None

    if not header:
        return None

    # Very small parser: keep stable and dependency-free.
    langs: list[tuple[str, float]] = []
    for raw_part in header.split(","):
        part = raw_part.strip()
        if not part:
            continue

        if ";q=" in part:
            lang_part, q_part = part.split(";q=", 1)
            try:
                quality = float(q_part)
            except ValueError:
                quality = 1.0
        else:
            lang_part = part
            quality = 1.0

        langs.append((lang_part.strip().lower(), quality))

    langs.sort(key=lambda item: item[1], reverse=True)

    for lang_part, _quality in langs:
        if lang_part.startswith("ja"):
            return "ja"
        if lang_part.startswith("en"):
            return "en"

    return None


def detect_lang(request) -> str:
    query_lang = _get_query_lang(request)
    if query_lang:
        return query_lang

    cookie_lang = _get_cookie_lang(request)
    if cookie_lang:
        return cookie_lang

    accept_language = _get_accept_language(request)
    if accept_language:
        return accept_language

    return DEFAULT_LANG


def get_translations(lang: str) -> dict[str, str]:
    return TRANSLATIONS.get(normalize_lang(lang), TRANSLATIONS[DEFAULT_LANG])


def t(lang: str, key: str, default: str | None = None) -> str:
    translations = get_translations(lang)
    if key in translations:
        return translations[key]

    fallback_translations = TRANSLATIONS[DEFAULT_LANG]
    if key in fallback_translations:
        return fallback_translations[key]

    if default is not None:
        return default

    return key


def status_label(lang: str, status: str | None) -> str:
    if not status:
        return t(lang, "common.none")

    return t(lang, f"status.{status}", status)


def confirmation_status_label(lang: str, status: str | None) -> str:
    if not status:
        return t(lang, "common.none")

    return t(lang, f"confirmation.{status}", status)


def realtime_status_label(lang: str, status: str | None) -> str:
    if not status:
        return t(lang, "realtime.rest_only")

    return t(lang, f"realtime.{status}", status)


def source_status_label(lang: str, status: str | None) -> str:
    if not status:
        return t(lang, "source.unknown")

    normalized = status.replace("-", "_")
    return t(lang, f"source.{normalized}", status)


def make_lang_url(request, lang: str) -> str:
    lang = normalize_lang(lang)

    url = str(request.url)
    split = urlsplit(url)

    query_items = dict(parse_qsl(split.query, keep_blank_values=True))
    query_items["lang"] = lang

    new_query = urlencode(query_items, doseq=True)
    return urlunsplit((split.scheme, split.netloc, split.path, new_query, split.fragment))


def js_translations(lang: str) -> dict[str, str]:
    # Keep this intentionally small.
    # Do not embed the full translation dictionary in HTML because internal keys
    # such as "status.simulated" can break rendered-HTML safety tests.
    keys = {
        "common.error",
        "common.success",
        "common.loading",
        "common.none",
        "demo.success",
        "demo.error",
        "wallet.add_success",
        "wallet.reset_success",
        "result.check",
        "result.summary",
        "result.pending",
        "result.unknown",
        "result.win",
        "result.loss",
        "result.rest_confirmed",
        "result.ws_unconfirmed",
        "result.ws_rest_conflict",
        "realtime.rest_only",
        "realtime.ws_live",
        "realtime.ws_stale",
    }
    return {key: t(lang, key) for key in sorted(keys)}

def template_i18n_context(request) -> dict:
    lang = detect_lang(request)

    return {
        "lang": lang,
        "js_i18n": js_translations(lang),
        "lang_url_ja": make_lang_url(request, "ja"),
        "lang_url_en": make_lang_url(request, "en"),
        "t": lambda key, default=None: t(lang, key, default),
        "status_label": lambda status: status_label(lang, status),
        "confirmation_status_label": lambda status: confirmation_status_label(lang, status),
        "realtime_status_label": lambda status: realtime_status_label(lang, status),
        "source_status_label": lambda status: source_status_label(lang, status),
    }
