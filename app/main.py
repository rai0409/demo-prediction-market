from __future__ import annotations

from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
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
from app.settlement import compare_candidate_with_rest_resolution, settle_pending_positions
from app.storage import (
    DEMO_USER_ID,
    count_resolution_candidates,
    connect,
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
    return str(value).replace("T", " ").replace("+00:00", " UTC").replace("Z", " UTC")


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


def template_context(request: Request, **extra):
    context = {
        "request": request,
        "disclaimer": DISCLAIMER,
        "poll_seconds": settings.poll_seconds,
        "ws_enabled": settings.ws_enabled,
        "demo_balance": get_balance(db, DEMO_USER_ID),
    }
    context.update(extra)
    return context


def data_status_badge(markets: list[dict]) -> str:
    status = markets[0].get("data_source_status") if markets else "sample_fallback"
    if status == "live":
        return "LIVE Polymarket"
    if status == "sample_fallback":
        return "Sample fallback"
    if status in {"live_failed_sample_fallback", "live_empty_sample_fallback"}:
        return "Live failed, sample fallback"
    return str(status)


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
        "confirmed_match": "REST確認済み",
        "rest_clear_without_candidate": "REST判定",
        "candidate_only_unconfirmed": "WSのみ未確認",
        "conflict": "WS/REST不一致",
        "unclear": "判定保留",
    }.get(status, "判定不明")


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
        item["ws_candidate_label"] = "WS検知あり" if candidate else "-"
        item["rest_confirmation_label"] = confirmation_label(confirmation["confirmation_status"])
        item["confirmation_status"] = confirmation["confirmation_status"]
        item["confirmation_note"] = item.get("settlement_note") or confirmation["note"]
    return enriched


def resolution_candidates_payload(conn: sqlite3.Connection) -> dict:
    candidates = list_resolution_candidate_updates(conn, limit=100)
    market_ids_with_candidates = list_markets_with_resolution_candidates(conn)
    results = list_demo_results(conn)
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
    all_markets = attach_realtime_updates(conn, ensure_fresh_markets(conn, settings), settings)
    market_response = filtered_market_response(all_markets)
    return templates.TemplateResponse(
        request,
        "index.html",
        template_context(
            request,
            markets=market_response["markets"],
            market_meta=market_response,
            data_status_badge=data_status_badge(all_markets),
            hidden_market_count=hidden_market_count(market_response),
        ),
    )


@app.get("/markets/{market_id}", response_class=HTMLResponse)
async def market_detail(request: Request, market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    market = attach_realtime_updates(conn, [market], settings)[0]
    market = enrich_market_for_display(market)
    snapshots = list_snapshots(conn, market_id, limit=12)
    return templates.TemplateResponse(
        request,
        "market_detail.html",
        template_context(request, market=market, snapshots=snapshots),
    )


@app.get("/demo-positions", response_class=HTMLResponse)
async def demo_positions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    results = {int(row["position_id"]): row for row in enrich_result_rows(conn, list_demo_results(conn))}
    positions = enrich_activity_rows(conn, list_positions(conn))
    for position in positions:
        result = results.get(int(position["id"]))
        position["result_status"] = result["status"] if result else "pending"
        position["result_status_label"] = result["status_label"] if result else "結果待ち"
    return templates.TemplateResponse(
        request,
        "demo_positions.html",
        template_context(
            request,
            positions=positions,
            orders=enrich_activity_rows(conn, list_orders(conn)),
            ledger=list_ledger(conn),
        ),
    )


@app.get("/demo-results", response_class=HTMLResponse)
async def demo_results(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    results = enrich_result_rows(conn, list_demo_results(conn))
    pending_count = sum(1 for row in results if row["status"] in {"pending", "settlement_pending", "settlement_unknown"})
    settled_count = sum(1 for row in results if row["status"] in {"settled_win", "settled_loss"})
    return templates.TemplateResponse(
        request,
        "demo_results.html",
        template_context(
            request,
            results=results,
            pending_count=pending_count,
            settled_count=settled_count,
        ),
    )


@app.get("/demo-wallet", response_class=HTMLResponse)
async def demo_wallet_page(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    snapshot = wallet_snapshot(conn)
    return templates.TemplateResponse(
        request,
        "demo_wallet.html",
        template_context(
            request,
            ledger=snapshot["ledger"],
            audit_events=snapshot["audit_events"],
            wallet_summary=snapshot["summary"],
        ),
    )


@app.get("/health")
async def health():
    return {"ok": True, "title": app.title}


@app.get("/api/markets")
async def api_markets(
    include_closed: bool = False,
    include_expired: bool = False,
    include_inactive: bool = False,
    include_all: bool = False,
    conn: sqlite3.Connection = Depends(get_conn),
):
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
async def api_refresh(conn: sqlite3.Connection = Depends(get_conn)):
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
async def api_debug_source_status(conn: sqlite3.Connection = Depends(get_conn)):
    return source_status(conn, settings)


@app.get("/api/realtime/status")
async def api_realtime_status(conn: sqlite3.Connection = Depends(get_conn)):
    return realtime_status(conn, settings)


@app.get("/api/demo/balance")
async def api_demo_balance(conn: sqlite3.Connection = Depends(get_conn)):
    return {"user_id": DEMO_USER_ID, "balance": get_balance(conn)}


@app.get("/api/demo/wallet")
async def api_demo_wallet(conn: sqlite3.Connection = Depends(get_conn)):
    return wallet_snapshot(conn)


@app.post("/api/demo/wallet/add-points")
async def api_demo_wallet_add_points(payload: AddDemoPointsRequest, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        return add_demo_points(
            conn,
            amount=payload.amount,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
    except DemoWalletError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.post("/api/demo/wallet/reset")
async def api_demo_wallet_reset(payload: ResetDemoBalanceRequest, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        return reset_demo_balance(
            conn,
            reason=payload.reason,
            idempotency_key=payload.idempotency_key,
        )
    except DemoWalletError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})


@app.get("/api/demo/positions")
async def api_demo_positions(conn: sqlite3.Connection = Depends(get_conn)):
    return {
        "balance": get_balance(conn),
        "positions": list_positions(conn),
        "orders": list_orders(conn),
        "ledger": list_ledger(conn),
    }


@app.get("/api/demo/results")
async def api_demo_results(conn: sqlite3.Connection = Depends(get_conn)):
    results = enrich_result_rows(conn, list_demo_results(conn))
    pending_count = sum(1 for row in results if row["status"] in {"pending", "settlement_pending", "settlement_unknown"})
    settled_count = sum(1 for row in results if row["status"] in {"settled_win", "settled_loss"})
    return {
        "balance": get_balance(conn),
        "results": results,
        "pending_count": pending_count,
        "settled_count": settled_count,
    }


@app.get("/api/demo/resolution-candidates")
async def api_demo_resolution_candidates(conn: sqlite3.Connection = Depends(get_conn)):
    return resolution_candidates_payload(conn)


@app.post("/api/demo/settle")
async def api_demo_settle(conn: sqlite3.Connection = Depends(get_conn)):
    if settings.live:
        ensure_fresh_markets(conn, settings)
    else:
        ensure_markets(conn, settings)
    return settle_pending_positions(conn, DEMO_USER_ID)


@app.post("/api/demo/predict")
async def api_demo_predict(payload: PredictionRequest, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        result = create_demo_prediction(
            conn,
            market_id=payload.market_id,
            outcome=payload.outcome,
            stake=payload.stake,
            idempotency_key=payload.idempotency_key,
        )
    except DemoPredictionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
    result["message"] = "デモ参加を記録しました。"
    return result
