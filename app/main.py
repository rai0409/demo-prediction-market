from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone
from secrets import compare_digest, token_urlsafe
from time import monotonic
from urllib.parse import parse_qs

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import sqlite3

from app.config import Settings, get_settings
from app.demo_points import DemoPredictionError, create_demo_prediction
from app.demo_wallet import DemoWalletError, add_demo_points, reset_demo_balance, wallet_snapshot
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
from app.storage import (
    DEMO_USER_ID,
    count_resolution_candidates,
    connect,
    ensure_demo_user,
    get_balance,
    get_latest_resolution_candidate,
    get_market,
    init_db,
    list_markets_with_resolution_candidates,
    list_ledger,
    list_demo_results,
    list_markets,
    list_orders,
    list_positions,
    list_resolution_candidate_updates,
    list_snapshots,
    normalize_demo_user_id,
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


async def get_conn() -> sqlite3.Connection:
    return db


DEMO_USER_COOKIE = "demo_user_id"
DEMO_USER_HEADER = "x-demo-user"
CSRF_COOKIE = "demo_csrf"
CSRF_HEADER = "x-csrf-token"
ADMIN_HEADER = "x-demo-admin-token"
RATE_LIMIT_WINDOW_SECONDS = 1.0
RATE_LIMIT_MAX_POSTS = 3
_post_rate_events: dict[tuple[str, str], list[float]] = {}


def current_demo_user_id(request: Request, conn: sqlite3.Connection) -> str:
    user_id = (
        request.query_params.get("demo_user")
        or request.headers.get(DEMO_USER_HEADER)
        or request.cookies.get(DEMO_USER_COOKIE)
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
    )
    return response


def set_demo_user_cookie_if_needed(response, request: Request, user_id: str):
    if request.query_params.get("demo_user") or request.headers.get(DEMO_USER_HEADER):
        response.set_cookie(
            DEMO_USER_COOKIE,
            user_id,
            max_age=60 * 60 * 24 * 30,
            httponly=True,
            samesite="lax",
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
        )
    return response


def require_csrf(request: Request) -> JSONResponse | None:
    cookie_token = request.cookies.get(CSRF_COOKIE)
    request_token = request.headers.get(CSRF_HEADER) or request.query_params.get("csrf_token")
    if not cookie_token or not request_token or not compare_digest(cookie_token, request_token):
        return JSONResponse(status_code=403, content={"detail": "操作を確認できませんでした。ページを再読み込みしてください。"})
    return None


def require_admin(request: Request) -> JSONResponse | None:
    if not settings.admin_token:
        return JSONResponse(status_code=403, content={"detail": "内部操作は現在利用できません。"})
    supplied = request.headers.get(ADMIN_HEADER) or request.query_params.get("admin_token")
    if not supplied or not compare_digest(settings.admin_token, supplied):
        return JSONResponse(status_code=403, content={"detail": "内部操作は許可されていません。"})
    return None


def rate_limit_post(user_id: str, action: str) -> JSONResponse | None:
    now = monotonic()
    key = (user_id, action)
    recent = [timestamp for timestamp in _post_rate_events.get(key, []) if now - timestamp < RATE_LIMIT_WINDOW_SECONDS]
    if len(recent) >= RATE_LIMIT_MAX_POSTS:
        _post_rate_events[key] = recent
        return JSONResponse(status_code=429, content={"detail": "少し時間をおいてからもう一度お試しください。"})
    recent.append(now)
    _post_rate_events[key] = recent
    return None


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


@app.get("/markets/{market_id}", response_class=HTMLResponse)
async def market_detail(request: Request, market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    user_id = current_demo_user_id(request, conn)
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    market = attach_realtime_updates(conn, [market], settings)[0]
    market = enrich_market_for_display(market)
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


@app.post("/demo-user")
async def set_demo_user(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    csrf_error = require_csrf(request)
    if csrf_error:
        return csrf_error
    body = (await request.body()).decode("utf-8")
    form = parse_qs(body)
    user_id = ensure_demo_user(conn, normalize_demo_user_id(form.get("demo_user", [""])[0]))
    lang = detect_lang(request)
    response = RedirectResponse(url=f"/?lang={lang}", status_code=303)
    response.set_cookie(
        DEMO_USER_COOKIE,
        user_id,
        max_age=60 * 60 * 24 * 30,
        httponly=True,
        samesite="lax",
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
