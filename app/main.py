from __future__ import annotations

import csv
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import StringIO
from secrets import compare_digest, token_urlsafe
from time import monotonic
from urllib.parse import urlencode, parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import sqlite3

from app.config import Settings, get_settings
from app.demo_points import DemoPredictionError, create_demo_prediction
from app.demo_wallet import DemoWalletError, add_demo_points, reset_demo_balance, reverse_demo_ledger_entry, wallet_snapshot
from app.market_display import enrich_market_for_display, filtered_market_response
from app.realtime import (
    attach_realtime_updates,
    ensure_fresh_markets,
    ensure_markets,
    realtime_status,
    refresh_markets_with_result,
    source_status,
)
from app.safety import DISCLAIMER
from app.i18n import detect_lang, template_i18n_context
from app.settlement import compare_candidate_with_rest_resolution, settle_pending_positions
from app.translation import add_translation_display, add_translation_displays
from app.storage import (
    DEMO_USER_ID,
    count_resolution_candidates,
    connect,
    ensure_demo_user,
    get_balance,
    get_latest_resolution_candidate,
    get_market,
    init_db,
    list_admin_audit_events,
    list_admin_ledger_entries,
    list_admin_settlements,
    list_demo_user_overview,
    list_markets_with_resolution_candidates,
    list_ledger,
    list_market_catalog,
    list_demo_results,
    list_markets,
    list_markets_by_ids,
    list_orders,
    list_positions,
    list_resolution_candidate_updates,
    list_snapshots,
    normalize_demo_user_id,
    verify_audit_chain,
)


settings = get_settings()
db = connect(settings.db_path)
templates = Jinja2Templates(directory="app/templates")


def format_percent(value: float | int | str | None) -> str:
    try:
        return f"{float(value) * 100:.1f}%"
    except (TypeError, ValueError):
        return "-"


def format_number(value: float | int | str | None) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return "-"
    if abs(number) >= 1_000_000:
        return f"{number / 1_000_000:.2f}M"
    if abs(number) >= 1_000:
        return f"{number / 1_000:.1f}K"
    return f"{number:.2f}"


def format_datetime(value: str | None) -> str:
    if not value:
        return "-"
    text = str(value).strip()
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return text.replace("T", " ")[:16]
    return parsed.strftime("%Y-%m-%d %H:%M")


templates.env.filters["percent"] = format_percent
templates.env.filters["number"] = format_number
templates.env.filters["date"] = format_datetime


@asynccontextmanager
async def lifespan(app: FastAPI):
    init_db(db)
    ensure_markets(db, settings)
    yield


app = FastAPI(title="Demo Prediction Market Viewer", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="app/static"), name="static")


class PredictionRequest(BaseModel):
    market_id: str
    outcome: str
    stake: float | str
    idempotency_key: str | None = None


class AddDemoPointsRequest(BaseModel):
    amount: float | str
    reason: str | None = None
    idempotency_key: str | None = None


class ResetDemoBalanceRequest(BaseModel):
    reason: str | None = None
    idempotency_key: str | None = None


class LedgerReversalRequest(BaseModel):
    ledger_entry_id: int
    reason: str
    idempotency_key: str | None = None


async def get_conn() -> sqlite3.Connection:
    return db


DEMO_USER_COOKIE = "demo_user_id"
DEMO_USER_HEADER = "x-demo-user"
CSRF_COOKIE = "demo_csrf"
CSRF_HEADER = "x-csrf-token"
ADMIN_HEADER = "x-demo-admin-token"
ADMIN_COOKIE = "demo_admin_token"
RATE_LIMIT_WINDOW_SECONDS = 1.0
RATE_LIMIT_MAX_POSTS = 3
ADMIN_DEFAULT_LIMIT = 50
ADMIN_MAX_LIMIT = 200
_post_rate_events: dict[tuple[str, str], list[float]] = {}
_operation_rejections: list[dict[str, str]] = []


def demo_session_cookie_name() -> str:
    return settings.session_cookie_name or DEMO_USER_COOKIE


def allowed_participant_codes() -> set[str]:
    return {
        normalize_demo_user_id(code)
        for code in settings.participant_codes.split(",")
        if normalize_demo_user_id(code)
    }


def validate_strict_participant_code(raw_code: str | None) -> str | None:
    code = normalize_demo_user_id(raw_code)
    allowed = allowed_participant_codes()
    if not allowed or code not in allowed:
        return None
    return code


def current_demo_user_id(request: Request, conn: sqlite3.Connection) -> str:
    cookie_name = demo_session_cookie_name()

    # Development/test-only participant override. Disabled by default.
    raw_header_override = (
        request.headers.get(DEMO_USER_HEADER)
        if settings.allow_demo_user_header
        else None
    )

    if settings.strict_participant_access:
        if raw_header_override:
            user_id = validate_strict_participant_code(raw_header_override)
            if user_id is None:
                raise HTTPException(
                    status_code=403,
                    detail="participant code is not allowed for this demo",
                )
            return ensure_demo_user(conn, user_id)

        cookie_user_id = request.cookies.get(cookie_name)
        if cookie_user_id:
            user_id = validate_strict_participant_code(cookie_user_id)
            if user_id is None:
                raise HTTPException(
                    status_code=403,
                    detail="participant session is not allowed for this demo",
                )
            return ensure_demo_user(conn, user_id)

        return ensure_demo_user(conn, DEMO_USER_ID)

    # Normal product requests do not accept participant identity from query
    # parameters. Identity comes from the existing participant cookie/session.
    user_id = (
        raw_header_override
        or request.cookies.get(cookie_name)
        or DEMO_USER_ID
    )
    return ensure_demo_user(conn, normalize_demo_user_id(user_id))


def csrf_token_for_request(request: Request) -> str:
    existing = getattr(request.state, "csrf_token", None)
    if existing:
        return existing
    token = request.cookies.get(CSRF_COOKIE) or token_urlsafe(24)
    request.state.csrf_token = token
    return token


def set_csrf_cookie(response, token: str):
    response.set_cookie(
        CSRF_COOKIE,
        token,
        max_age=60 * 60 * 24,
        httponly=False,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


def set_demo_user_cookie_if_needed(response, request: Request, user_id: str):
    if settings.allow_demo_user_header and request.headers.get(DEMO_USER_HEADER):
        response.set_cookie(
            demo_session_cookie_name(),
            user_id,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
            secure=settings.cookie_secure,
        )
    return response


def prepare_response(response, request: Request, user_id: str):
    set_lang_cookie_if_needed(response, request)
    set_demo_user_cookie_if_needed(response, request, user_id)
    set_csrf_cookie(response, csrf_token_for_request(request))
    return response


def template_context(request: Request, conn: sqlite3.Connection, user_id: str, **extra):
    context = {
        "request": request,
        "disclaimer": DISCLAIMER,
        "poll_seconds": settings.poll_seconds,
        "quick_refresh_seconds": settings.quick_refresh_seconds,
        "detail_refresh_seconds": settings.detail_refresh_seconds,
        "ws_enabled": settings.ws_enabled,
        "demo_user_id": user_id,
        "demo_balance": get_balance(conn, user_id),
        "csrf_token": csrf_token_for_request(request),
        "admin_enabled": bool(settings.admin_token),
    }
    context.update(template_i18n_context(request))
    context.update(extra)
    return context


def set_lang_cookie_if_needed(response, request: Request):
    lang = detect_lang(request)
    requested_lang = request.query_params.get("lang")
    if requested_lang in {"ja", "en"}:
        response.set_cookie(
            "demo_lang",
            lang,
            max_age=60 * 60 * 24 * 365,
            httponly=False,
            samesite="lax",
            secure=settings.cookie_secure,
        )
    return response


def require_csrf(request: Request) -> JSONResponse | None:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    request_token = request.headers.get(CSRF_HEADER) or request.query_params.get("csrf_token")
    if not cookie_token or not request_token or not compare_digest(cookie_token, request_token):
        record_operation_rejection(request, "csrf", "操作確認に失敗")
        return JSONResponse(status_code=403, content={"detail": "操作を確認できませんでした。ページを再読み込みしてください。"})
    return None


def admin_token_from_request(request: Request) -> str | None:
    return (
        request.headers.get(ADMIN_HEADER)
        or request.cookies.get(ADMIN_COOKIE)
    )


def is_admin_request(request: Request) -> bool:
    if not settings.admin_token:
        return False
    supplied = admin_token_from_request(request)
    return bool(supplied and compare_digest(settings.admin_token, supplied))


def require_admin(request: Request) -> JSONResponse | None:
    if not settings.admin_token:
        record_operation_rejection(request, "admin", "管理コード未設定")
        return JSONResponse(status_code=403, content={"detail": "内部操作は現在利用できません。"})
    if not is_admin_request(request):
        record_operation_rejection(request, "admin", "管理コード不一致")
        return JSONResponse(status_code=403, content={"detail": "内部操作は許可されていません。"})
    return None


def record_operation_rejection(request: Request, category: str, reason: str) -> None:
    participant = request.cookies.get(demo_session_cookie_name()) or "-"

    if settings.allow_demo_user_header:
        participant = request.headers.get(DEMO_USER_HEADER) or participant

    _operation_rejections.append(
        {
            "時刻": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "種別": category,
            "理由": reason,
            "経路": request.url.path,
            "参加者": participant,
        }
    )
    del _operation_rejections[:-100]


def rate_limit_post(user_id: str, action: str) -> JSONResponse | None:
    now = monotonic()
    key = (user_id, action)
    recent = [timestamp for timestamp in _post_rate_events.get(key, []) if now - timestamp < RATE_LIMIT_WINDOW_SECONDS]
    if len(recent) >= RATE_LIMIT_MAX_POSTS:
        _post_rate_events[key] = recent
        record_operation_rejection_placeholder(user_id, action, "連続操作")
        return JSONResponse(status_code=429, content={"detail": "少し時間をおいてからもう一度お試しください。"})
    recent.append(now)
    _post_rate_events[key] = recent
    return None


def record_operation_rejection_placeholder(user_id: str, action: str, reason: str) -> None:
    _operation_rejections.append(
        {
            "時刻": datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M"),
            "種別": action,
            "理由": reason,
            "経路": "-",
            "参加者": user_id,
        }
    )
    del _operation_rejections[:-100]


def _positive_int(value: str | None, default: int, maximum: int | None = None) -> int:
    try:
        parsed = int(value or "")
    except ValueError:
        parsed = default
    if parsed < 1:
        parsed = default
    if maximum is not None:
        parsed = min(parsed, maximum)
    return parsed


def _date_boundary(value: str | None, end_of_day: bool = False) -> str:
    if not value:
        return ""
    try:
        parsed = datetime.strptime(value.strip(), "%Y-%m-%d")
    except ValueError:
        return ""
    suffix = "23:59:59" if end_of_day else "00:00:00"
    return f"{parsed.strftime('%Y-%m-%d')} {suffix}"


def admin_filters(request: Request) -> dict[str, str | int]:
    page = _positive_int(request.query_params.get("page"), 1)
    limit = _positive_int(request.query_params.get("limit"), ADMIN_DEFAULT_LIMIT, ADMIN_MAX_LIMIT)
    return {
        "user_id": (request.query_params.get("participant") or request.query_params.get("user_id") or "").strip(),
        "event_type": (request.query_params.get("event_type") or "").strip(),
        "market_id": (request.query_params.get("market_id") or "").strip(),
        "position_id": (request.query_params.get("position_id") or "").strip(),
        "settled": (request.query_params.get("settled") or "").strip(),
        "date_from": (request.query_params.get("date_from") or request.query_params.get("from") or "").strip(),
        "date_to": (request.query_params.get("date_to") or request.query_params.get("to") or "").strip(),
        "date_from_sql": _date_boundary(request.query_params.get("date_from") or request.query_params.get("from")),
        "date_to_sql": _date_boundary(request.query_params.get("date_to") or request.query_params.get("to"), end_of_day=True),
        "page": page,
        "limit": limit,
        "offset": (page - 1) * limit,
    }


def admin_query(filters: dict[str, str | int], *, page_delta: int = 0, export_type: str | None = None) -> str:
    page = max(1, int(filters["page"]) + page_delta)
    values = {
        "participant": filters["user_id"],
        "event_type": filters["event_type"],
        "market_id": filters["market_id"],
        "position_id": filters["position_id"],
        "settled": filters["settled"],
        "date_from": filters["date_from"],
        "date_to": filters["date_to"],
        "page": page,
        "limit": filters["limit"],
    }
    if export_type:
        values["type"] = export_type
    return urlencode({key: value for key, value in values.items() if value not in ("", None)})


CATALOG_QUERY_MAX_LENGTH = 120
CATALOG_STATUSES = {"active", "closed", "all"}
CATALOG_SORTS = {"volume_24h", "liquidity", "end_date", "probability", "updated"}
CATALOG_PAGE_SIZES = {10, 20, 50}


def catalog_filters(request: Request) -> dict[str, str | int]:
    query = (request.query_params.get("q") or "").strip()[:CATALOG_QUERY_MAX_LENGTH]
    status = (request.query_params.get("status") or "active").strip().lower()
    sort = (request.query_params.get("sort") or "volume_24h").strip().lower()
    order = (request.query_params.get("order") or "desc").strip().lower()
    try:
        page_size_raw = int(request.query_params.get("page_size") or "20")
    except ValueError:
        page_size_raw = 20
    return {
        "q": query,
        "status": status if status in CATALOG_STATUSES else "active",
        "sort": sort if sort in CATALOG_SORTS else "volume_24h",
        "order": order if order in {"asc", "desc"} else "desc",
        "page": _positive_int(request.query_params.get("page"), 1),
        "page_size": page_size_raw if page_size_raw in CATALOG_PAGE_SIZES else 20,
    }


def catalog_url(filters: dict[str, str | int], *, page: int | None = None, **changes: str | int | None) -> str:
    values = dict(filters)
    values.update(changes)
    if page is not None:
        values["page"] = page
    values["lang"] = values.get("lang") or "ja"
    return "/markets?" + urlencode({key: value for key, value in values.items() if value not in ("", None)})


def catalog_market_is_active(market: dict) -> bool:
    if not market.get("active") or market.get("closed"):
        return False
    value = market.get("end_date")
    if not value:
        return True
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed > datetime.now(timezone.utc)
    except ValueError:
        return True


def catalog_question(value: str | None, maximum: int = 220) -> str:
    text = (value or "").strip()
    return text if len(text) <= maximum else f"{text[:maximum - 1].rstrip()}…"


def data_status_badge(markets: list[dict]) -> str:
    status = markets[0].get("data_source_status") if markets else "sample_fallback"
    if status == "live":
        return "外部参考データ"
    if status == "sample_fallback":
        return "参考データ"
    if status in {"live_failed_sample_fallback", "live_empty_sample_fallback"}:
        return "参考データ"
    return "参考データ"


def hidden_market_count(meta: dict) -> int:
    keys = (
        "hidden_closed_count",
        "hidden_inactive_count",
        "hidden_expired_count",
        "hidden_no_liquidity_count",
        "hidden_resolved_probability_count",
    )
    return sum(int(meta.get(key, 0) or 0) for key in keys)


def enrich_activity_rows(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    enriched: list[dict] = []
    for row in rows:
        item = dict(row)
        market = get_market(conn, item.get("market_id", ""))
        item["market_title"] = market.get("title") if market else item.get("market_id", "")
        item["market_question"] = market.get("question") if market else ""
        enriched.append(item)
    return enriched


def result_status_label(status: str) -> str:
    return {
        "pending": "結果待ち",
        "settlement_pending": "判定保留",
        "settlement_unknown": "判定不明",
        "settled_win": "的中",
        "settled_loss": "不的中",
    }.get(status, status)


def confirmation_label(status: str) -> str:
    return {
        "confirmed_match": "参考データ確認済み",
        "rest_clear_without_candidate": "参考データ判定",
        "candidate_only_unconfirmed": "結果候補のみ",
        "conflict": "参考データ不一致",
        "unclear": "判定保留",
    }.get(status, "判定不明")


def source_label(source: str | None) -> str:
    return {
        "ws_candidate_rest_confirmed": "結果候補と参考データが一致",
        "rest_conservative": "参考データで明確に確認",
        "ws_candidate_unconfirmed": "結果候補のみ。参考データ確認待ち",
        "ws_candidate_conflict": "結果候補と参考データが不一致",
        "unresolved": "参考データ確認待ち",
        "local_demo": "結果確認待ち",
        "local_storage": "保存データ確認不可",
    }.get(source or "", "参考データ確認待ち")


def result_reason_label(status: str, confirmation_status: str, note: str | None) -> str:
    if status in {"settled_win", "settled_loss"}:
        return "明確な結果を確認したため、参考スコアへ反映しました。"
    if confirmation_status == "candidate_only_unconfirmed":
        return "結果候補はありますが、参考データでの確認が未完了です。"
    if confirmation_status == "conflict":
        return "結果候補と参考データが一致しないため、結果記録を保留しています。"
    if confirmation_status == "unclear":
        return "明確な結果をまだ確認できていません。"
    return note or "結果確認待ちです。"


def operation_label(event_type: str | None) -> str:
    return {
        "demo_prediction_created": "デモ参加記録",
        "demo_prediction_replayed": "デモ参加の重複確認",
        "demo_point_add_created": "デモポイント調整",
        "demo_point_add_replayed": "デモポイント調整の重複確認",
        "demo_balance_reset_created": "初期状態に戻す操作",
        "demo_balance_reset_replayed": "初期状態操作の重複確認",
        "settlement_paid": "参考スコア反映",
        "settlement_loss": "結果記録",
        "settlement_checked": "結果確認",
        "settlement_ws_candidate_confirmed": "結果候補と参考データ一致",
        "settlement_ws_candidate_unconfirmed": "結果候補の確認待ち",
        "settlement_ws_candidate_conflict": "結果候補と参考データ不一致",
        "settlement_rest_conservative": "参考データで確認",
    }.get(event_type or "", event_type or "-")


def history_type_label(entry_type: str | None) -> str:
    return {
        "initial": "初期付与",
        "prediction": "デモ参加利用",
        "demo_point_add": "調整",
        "demo_balance_reset": "初期状態に戻す",
        "settlement_win": "参考スコア反映",
        "settlement_loss": "結果記録",
    }.get(entry_type or "", entry_type or "-")


def enrich_admin_rows(
    audit_events: list[dict],
    ledger_entries: list[dict],
    settlements: list[dict],
) -> tuple[list[dict], list[dict], list[dict]]:
    for event in audit_events:
        event["operation_label"] = operation_label(event.get("event_type"))
    for entry in ledger_entries:
        entry["history_label"] = history_type_label(entry.get("entry_type"))
    for row in settlements:
        row["status_label"] = result_status_label(row.get("status", ""))
    return audit_events, ledger_entries, settlements


def admin_review_rows(conn: sqlite3.Connection, filters: dict[str, str | int]) -> tuple[list[dict], list[dict], list[dict]]:
    reference_id = str(filters["position_id"])
    audit_events = list_admin_audit_events(
        conn,
        user_id=str(filters["user_id"]) or None,
        event_type=str(filters["event_type"]) or None,
        reference_id=reference_id or None,
        date_from=str(filters["date_from_sql"]) or None,
        date_to=str(filters["date_to_sql"]) or None,
        limit=int(filters["limit"]),
        offset=int(filters["offset"]),
    )
    ledger_entries = list_admin_ledger_entries(
        conn,
        user_id=str(filters["user_id"]) or None,
        market_id=str(filters["market_id"]) or None,
        reference_id=reference_id or None,
        date_from=str(filters["date_from_sql"]) or None,
        date_to=str(filters["date_to_sql"]) or None,
        limit=int(filters["limit"]),
        offset=int(filters["offset"]),
    )
    settlements = list_admin_settlements(
        conn,
        user_id=str(filters["user_id"]) or None,
        market_id=str(filters["market_id"]) or None,
        position_id=str(filters["position_id"]) or None,
        settled=str(filters["settled"]) or None,
        date_from=str(filters["date_from_sql"]) or None,
        date_to=str(filters["date_to_sql"]) or None,
        limit=int(filters["limit"]),
        offset=int(filters["offset"]),
    )
    return enrich_admin_rows(audit_events, ledger_entries, settlements)


def csv_safe(value) -> str:
    text = "" if value is None else str(value)
    if text.startswith(("=", "+", "-", "@")):
        return "'" + text
    return text


def csv_response(filename: str, headers: list[str], rows: list[dict]) -> Response:
    buffer = StringIO()
    writer = csv.writer(buffer)
    writer.writerow(headers)
    for row in rows:
        writer.writerow([csv_safe(row.get(header, "")) for header in headers])
    return Response(
        content=buffer.getvalue(),
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


def enrich_result_rows(conn: sqlite3.Connection, rows: list[dict]) -> list[dict]:
    enriched = enrich_activity_rows(conn, rows)
    for item in enriched:
        item["status_label"] = result_status_label(item.get("status", ""))
        item["winning_outcome_label"] = item.get("winning_outcome") or "-"
        market = get_market(conn, item.get("market_id", ""))
        candidate = get_latest_resolution_candidate(conn, item.get("market_id", ""))
        confirmation = compare_candidate_with_rest_resolution(candidate, market or {}) if market else {
            "candidate_winning_outcome": candidate.get("winning_outcome") if candidate else None,
            "candidate_winning_asset_id": candidate.get("winning_asset_id") if candidate else None,
            "rest_winning_outcome": None,
            "confirmation_status": "unclear",
            "settlement_source": item.get("settlement_source") or "unresolved",
            "note": item.get("settlement_note") or "判定保留",
        }
        item["ws_candidate_detected"] = bool(candidate)
        item["ws_candidate_label"] = "結果候補あり" if candidate else "-"
        item["rest_confirmation_label"] = confirmation_label(confirmation["confirmation_status"])
        item["confirmation_status"] = confirmation["confirmation_status"]
        item["confirmation_note"] = item.get("settlement_note") or confirmation["note"]
        item["source_label"] = source_label(item.get("settlement_source") or confirmation["settlement_source"])
        item["result_reason_label"] = result_reason_label(
            item.get("status", ""),
            confirmation["confirmation_status"],
            item.get("settlement_note") or confirmation["note"],
        )
    return enriched


def resolution_candidates_payload(conn: sqlite3.Connection, user_id: str) -> dict:
    candidates = list_resolution_candidate_updates(conn, limit=100)
    market_ids_with_candidates = list_markets_with_resolution_candidates(conn)
    results = list_demo_results(conn, user_id)
    pending_market_ids = {
        row["market_id"]
        for row in results
        if row["status"] in {"pending", "settlement_pending", "settlement_unknown"}
    }
    serialized = []
    for candidate in candidates:
        serialized.append(
            {
                "market_id": candidate["market_id"],
                "winning_outcome": candidate["winning_outcome"],
                "winning_asset_id": candidate["winning_asset_id"],
                "event_timestamp": candidate["event_timestamp"],
                "received_at": candidate["received_at"],
                "confirmation_hint": {
                    "pending_demo_settlement_exists": candidate["market_id"] in pending_market_ids,
                },
            }
        )
    latest_by_market = {}
    for market_id in market_ids_with_candidates:
        candidate = get_latest_resolution_candidate(conn, market_id)
        if candidate:
            latest_by_market[market_id] = {
                "market_id": candidate["market_id"],
                "winning_outcome": candidate["winning_outcome"],
                "winning_asset_id": candidate["winning_asset_id"],
                "event_timestamp": candidate["event_timestamp"],
                "received_at": candidate["received_at"],
            }
    return {
        "candidate_count": count_resolution_candidates(conn),
        "markets_with_candidates_count": len(market_ids_with_candidates),
        "candidates": serialized,
        "latest_by_market": latest_by_market,
        "pending_settlement_market_ids": sorted(pending_market_ids),
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    all_markets = attach_realtime_updates(conn, ensure_fresh_markets(conn, settings), settings)
    all_markets = add_translation_displays(
        conn,
        all_markets,
        language=detect_lang(request),
        enabled=settings.translation_enabled,
    )
    market_response = filtered_market_response(all_markets)
    response = templates.TemplateResponse(
        request,
        "index.html",
        template_context(
            request,
            conn,
            user_id,
            markets=market_response["markets"],
            market_meta=market_response,
            data_status_badge=data_status_badge(all_markets),
            hidden_market_count=hidden_market_count(market_response),
        ),
    )
    return prepare_response(response, request, user_id)


@app.get("/markets", response_class=HTMLResponse)
async def market_catalog(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    filters = catalog_filters(request)
    filters["lang"] = detect_lang(request)
    ensure_fresh_markets(conn, settings)
    page = int(filters["page"])
    page_size = int(filters["page_size"])
    catalog = list_market_catalog(
        conn,
        str(filters["q"]),
        str(filters["status"]),
        str(filters["sort"]),
        str(filters["order"]),
        page_size,
        (page - 1) * page_size,
    )
    total_pages = int(catalog["total_pages"])
    if total_pages and page > total_pages:
        page = total_pages
        catalog = list_market_catalog(
            conn,
            str(filters["q"]),
            str(filters["status"]),
            str(filters["sort"]),
            str(filters["order"]),
            page_size,
            (page - 1) * page_size,
        )
    markets = attach_realtime_updates(conn, catalog["markets"], settings)
    markets = add_translation_displays(
        conn,
        markets,
        language=detect_lang(request),
        enabled=settings.translation_enabled,
    )
    rendered_markets = []
    for market in markets:
        item = enrich_market_for_display(market)
        item["catalog_status"] = "active" if catalog_market_is_active(item) else "closed"
        item["catalog_question"] = catalog_question(item.get("display_question"))
        rendered_markets.append(item)
    filters["page"] = page
    filter_urls = {status: catalog_url(filters, page=1, status=status) for status in ("active", "closed", "all")}
    sort_urls = {sort: catalog_url(filters, page=1, sort=sort) for sort in CATALOG_SORTS}
    response = templates.TemplateResponse(
        request,
        "markets.html",
        template_context(
            request,
            conn,
            user_id,
            markets=rendered_markets,
            q=filters["q"],
            status=filters["status"],
            sort=filters["sort"],
            order=filters["order"],
            page=page,
            page_size=page_size,
            total_count=catalog["total_count"],
            total_pages=total_pages,
            has_previous=page > 1,
            has_next=bool(total_pages and page < total_pages),
            previous_url=catalog_url(filters, page=page - 1),
            next_url=catalog_url(filters, page=page + 1),
            filter_urls=filter_urls,
            sort_urls=sort_urls,
            clear_filters_url=catalog_url({"lang": filters["lang"]}, page=1),
        ),
    )
    return prepare_response(response, request, user_id)


@app.get("/markets/{market_id}", response_class=HTMLResponse)
async def market_detail(request: Request, market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    market = attach_realtime_updates(conn, [market], settings)[0]
    market = enrich_market_for_display(market)
    market = add_translation_display(
        conn,
        market,
        language=detect_lang(request),
        enabled=settings.translation_enabled,
    )
    snapshots = list_snapshots(conn, market_id, limit=12)
    response = templates.TemplateResponse(
        request,
        "market_detail.html",
        template_context(request, conn, user_id, market=market, snapshots=snapshots),
    )
    return prepare_response(response, request, user_id)


@app.get("/demo-positions", response_class=HTMLResponse)
async def demo_positions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    results = {int(row["position_id"]): row for row in enrich_result_rows(conn, list_demo_results(conn, user_id))}
    positions = enrich_activity_rows(conn, list_positions(conn, user_id))
    for position in positions:
        result = results.get(int(position["id"]))
        position["result_status"] = result["status"] if result else "pending"
        position["result_status_label"] = result["status_label"] if result else "結果待ち"
    response = templates.TemplateResponse(
        request,
        "demo_positions.html",
        template_context(
            request,
            conn,
            user_id,
            positions=positions,
            orders=enrich_activity_rows(conn, list_orders(conn, user_id)),
            ledger=list_ledger(conn, user_id),
        ),
    )
    return prepare_response(response, request, user_id)


@app.get("/demo-results", response_class=HTMLResponse)
async def demo_results(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    results = enrich_result_rows(conn, list_demo_results(conn, user_id))
    pending_count = sum(1 for row in results if row["status"] in {"pending", "settlement_pending", "settlement_unknown"})
    settled_count = sum(1 for row in results if row["status"] in {"settled_win", "settled_loss"})
    response = templates.TemplateResponse(
        request,
        "demo_results.html",
        template_context(
            request,
            conn,
            user_id,
            results=results,
            pending_count=pending_count,
            settled_count=settled_count,
        ),
    )
    return prepare_response(response, request, user_id)


@app.get("/demo-wallet", response_class=HTMLResponse)
async def demo_wallet_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    snapshot = wallet_snapshot(conn, user_id)
    response = templates.TemplateResponse(
        request,
        "demo_wallet.html",
        template_context(
            request,
            conn,
            user_id,
            ledger=snapshot["ledger"],
            audit_events=snapshot["audit_events"],
            wallet_summary=snapshot["summary"],
        ),
    )
    return prepare_response(response, request, user_id)


@app.get("/admin/audit", response_class=HTMLResponse)
async def admin_audit_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    if not is_admin_request(request):
        response = templates.TemplateResponse(
            request,
            "admin_audit.html",
            template_context(
                request,
                conn,
                user_id,
                authorized=False,
                filters=admin_filters(request),
                users=[],
                audit_events=[],
                ledger_entries=[],
                settlements=[],
                rejected_operations=[],
            ),
            status_code=403,
        )
        return prepare_response(response, request, user_id)

    filters = admin_filters(request)
    users = list_demo_user_overview(conn)
    if filters["user_id"]:
        users = [user for user in users if user["user_id"] == filters["user_id"]]
    audit_events, ledger_entries, settlements = admin_review_rows(conn, filters)
    response = templates.TemplateResponse(
        request,
        "admin_audit.html",
        template_context(
            request,
            conn,
            user_id,
            authorized=True,
            filters=filters,
            users=users,
            audit_events=audit_events,
            ledger_entries=ledger_entries,
            settlements=settlements,
            rejected_operations=list(reversed(_operation_rejections[-50:])),
            query_current=admin_query(filters),
            query_prev=admin_query(filters, page_delta=-1),
            query_next=admin_query(filters, page_delta=1),
            export_audit_query=admin_query(filters, export_type="audit"),
            export_ledger_query=admin_query(filters, export_type="ledger"),
            export_settlements_query=admin_query(filters, export_type="settlements"),
        ),
    )
    return prepare_response(response, request, user_id)


@app.get("/admin/audit.csv")
async def admin_audit_csv(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    filters = admin_filters(request)
    audit_events, ledger_entries, settlements = admin_review_rows(conn, filters)
    export_type = (request.query_params.get("type") or "audit").strip()
    if export_type == "ledger":
        headers = [
            "created_at",
            "user_id",
            "market_id",
            "history_label",
            "amount",
            "balance_before",
            "balance_after",
            "reference_type",
            "reference_id",
            "note",
        ]
        return csv_response("admin-ledger.csv", headers, ledger_entries)
    if export_type == "settlements":
        headers = [
            "created_at",
            "user_id",
            "position_id",
            "market_id",
            "outcome",
            "status_label",
            "payout",
            "settlement_note",
            "settled_at",
        ]
        return csv_response("admin-settlements.csv", headers, settlements)
    headers = [
        "created_at",
        "user_id",
        "operation_label",
        "route",
        "reference_type",
        "reference_id",
        "note",
    ]
    return csv_response("admin-audit.csv", headers, audit_events)


@app.post("/admin/audit/access")
async def admin_audit_access(request: Request):
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    supplied = (form.get("admin_token", [""])[0] or "").strip()
    if not settings.admin_token or not supplied or not compare_digest(settings.admin_token, supplied):
        record_operation_rejection(request, "admin", "管理画面コード不一致")
        return JSONResponse(status_code=403, content={"detail": "内部確認画面は利用できません。"})
    response = RedirectResponse(url="/admin/audit", status_code=303)
    response.set_cookie(
        ADMIN_COOKIE,
        supplied,
        max_age=60 * 30,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


@app.post("/demo-user")
async def set_demo_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    if not settings.participant_switch_enabled:
        record_operation_rejection(request, "participant", "参加者切替無効")
        raise HTTPException(status_code=404, detail="not found")

    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    requested_user_id = form.get("demo_user", [""])[0]
    if settings.strict_participant_access:
        user_id = validate_strict_participant_code(requested_user_id)
        if user_id is None:
            record_operation_rejection(request, "participant", "参加者コード不一致")
            return JSONResponse(status_code=403, content={"detail": "このデモで許可された参加者コードを入力してください。"})
    else:
        user_id = normalize_demo_user_id(requested_user_id)
    user_id = ensure_demo_user(conn, user_id)
    lang = detect_lang(request)
    response = RedirectResponse(url=f"/?lang={lang}", status_code=303)
    response.set_cookie(
        demo_session_cookie_name(),
        user_id,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
        secure=settings.cookie_secure,
    )
    return response


@app.get("/health")
async def health():
    return {"ok": True, "title": app.title}


@app.get("/api/markets")
async def api_markets(
    request: Request,
    include_closed: bool = False,
    include_expired: bool = False,
    include_inactive: bool = False,
    include_all: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
):
    if include_all:
        admin_error = require_admin(request)
        if admin_error:
            return admin_error
    markets = attach_realtime_updates(conn, ensure_fresh_markets(conn, settings), settings)
    return filtered_market_response(
        markets,
        include_closed=include_closed,
        include_expired=include_expired,
        include_inactive=include_inactive,
        include_all=include_all,
    )


LIVE_MARKET_FIELDS = (
    "market_id",
    "probabilities",
    "volume_24hr",
    "liquidity",
    "best_bid",
    "best_ask",
    "last_trade_price",
    "realtime_spread",
    "updated_at",
    "live",
    "demo_participation_allowed",
)


def live_market_payload(market: dict) -> dict:
    enriched = enrich_market_for_display(market)
    updated_at = enriched.get("ws_last_event_at") or enriched.get("fetched_at")
    values = {
        **enriched,
        "updated_at": updated_at,
        "live": bool(enriched.get("active")) and not bool(enriched.get("closed")),
    }
    return {field: values.get(field) for field in LIVE_MARKET_FIELDS}


@app.get("/api/markets/updates")
async def api_market_updates(ids: str = "", conn: sqlite3.Connection = Depends(get_conn)):
    requested_ids = [part.strip() for part in ids.split(",") if part.strip()]
    if not requested_ids:
        raise HTTPException(status_code=400, detail="at least one market id is required")
    if len(requested_ids) > 50:
        raise HTTPException(status_code=400, detail="at most 50 market ids are allowed")
    market_ids = list(dict.fromkeys(requested_ids))
    markets = list_markets_by_ids(conn, market_ids)
    enriched = attach_realtime_updates(conn, markets, settings)
    return {"markets": [live_market_payload(market) for market in enriched]}


@app.get("/api/markets/{market_id}/live")
async def api_market_live(market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    enriched = attach_realtime_updates(conn, [market], settings)[0]
    return live_market_payload(enriched)


@app.get("/api/markets/{market_id}")
async def api_market(market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    market = attach_realtime_updates(conn, [market], settings)[0]
    return enrich_market_for_display(market)


@app.get("/api/markets/{market_id}/snapshots")
async def api_snapshots(market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return {"snapshots": list_snapshots(conn, market_id)}


@app.post("/api/refresh")
async def api_refresh(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    result = refresh_markets_with_result(conn, settings)
    return {
        "status": result["status"],
        "error": result["error"],
        "raw_count": result["raw_count"],
        "normalized_count": result["normalized_count"],
        "fallback_used": result["fallback_used"],
        "markets": result["markets"],
        "count": len(result["markets"]),
    }


@app.get("/api/debug/source-status")
async def api_debug_source_status(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    return source_status(conn, settings)


@app.get("/api/admin/audit/integrity")
async def api_admin_audit_integrity(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    return verify_audit_chain(conn)


@app.get("/api/realtime/status")
async def api_realtime_status(conn: sqlite3.Connection = Depends(get_conn)):
    return realtime_status(conn, settings)


@app.get("/api/demo/balance")
async def api_demo_balance(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    return {"user_id": user_id, "balance": get_balance(conn, user_id)}


@app.get("/api/demo/wallet")
async def api_demo_wallet(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    return wallet_snapshot(conn, user_id)


@app.post("/api/demo/wallet/add-points")
async def api_demo_wallet_add_points(
    request: Request,
    payload: AddDemoPointsRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    user_id = current_demo_user_id(request, conn)
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    rate_error = rate_limit_post(user_id, "demo-wallet-add")
    if rate_error:
        return rate_error
    try:
        return add_demo_points(
            conn,
            amount=payload.amount,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            user_id=user_id,
        )
    except DemoWalletError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.post("/api/demo/wallet/reset")
async def api_demo_wallet_reset(
    request: Request,
    payload: ResetDemoBalanceRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    user_id = current_demo_user_id(request, conn)
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    rate_error = rate_limit_post(user_id, "demo-wallet-reset")
    if rate_error:
        return rate_error
    try:
        return reset_demo_balance(
            conn,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
            user_id=user_id,
        )
    except DemoWalletError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.post("/api/demo/ledger/reversal")
async def api_demo_ledger_reversal(
    request: Request,
    payload: LedgerReversalRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    rate_error = rate_limit_post("admin", "demo-ledger-reversal")
    if rate_error:
        return rate_error
    try:
        return reverse_demo_ledger_entry(
            conn,
            ledger_entry_id=payload.ledger_entry_id,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
    except DemoWalletError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/api/demo/positions")
async def api_demo_positions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    return {
        "user_id": user_id,
        "balance": get_balance(conn, user_id),
        "positions": list_positions(conn, user_id),
        "orders": list_orders(conn, user_id),
        "ledger": list_ledger(conn, user_id),
    }


@app.get("/api/demo/results")
async def api_demo_results(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    results = enrich_result_rows(conn, list_demo_results(conn, user_id))
    pending_count = sum(1 for row in results if row["status"] in {"pending", "settlement_pending", "settlement_unknown"})
    settled_count = sum(1 for row in results if row["status"] in {"settled_win", "settled_loss"})
    return {
        "user_id": user_id,
        "balance": get_balance(conn, user_id),
        "results": results,
        "pending_count": pending_count,
        "settled_count": settled_count,
    }


@app.get("/api/demo/resolution-candidates")
async def api_demo_resolution_candidates(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    user_id = current_demo_user_id(request, conn)
    return resolution_candidates_payload(conn, user_id)


@app.post("/api/demo/settle")
async def api_demo_settle(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    admin_error = require_admin(request)
    if admin_error:
        return admin_error
    rate_error = rate_limit_post(user_id, "demo-settle")
    if rate_error:
        return rate_error
    if settings.live:
        ensure_fresh_markets(conn, settings)
    else:
        ensure_markets(conn, settings)
    return settle_pending_positions(conn, user_id)


@app.post("/api/demo/predict")
async def api_demo_predict(
    request: Request,
    payload: PredictionRequest,
    conn: sqlite3.Connection = Depends(get_conn),
):
    user_id = current_demo_user_id(request, conn)
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    rate_error = rate_limit_post(user_id, "demo-predict")
    if rate_error:
        return rate_error
    try:
        result = create_demo_prediction(
            conn,
            market_id=payload.market_id,
            outcome=payload.outcome,
            stake=payload.stake,
            idempotency_key=payload.idempotency_key,
            user_id=user_id,
            max_stake=settings.max_demo_stake,
        )
    except DemoPredictionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
    result["message"] = "デモ参加を記録しました。"
    return result
