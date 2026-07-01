from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import Depends, FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
import sqlite3

from app.config import Settings, get_settings
from app.demo_points import DemoPredictionError, create_demo_prediction
from app.market_display import enrich_market_for_display, filtered_market_response
from app.realtime import ensure_markets, refresh_markets_with_result, source_status
from app.safety import DISCLAIMER
from app.storage import (
    DEMO_USER_ID,
    connect,
    get_balance,
    get_market,
    init_db,
    list_ledger,
    list_markets,
    list_orders,
    list_positions,
    list_snapshots,
)


settings = get_settings()
db = connect(settings.db_path)
templates = Jinja2Templates(directory="app/templates")


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


async def get_conn() -> sqlite3.Connection:
    return db


def template_context(request: Request, **extra):
    context = {
        "request": request,
        "disclaimer": DISCLAIMER,
        "poll_seconds": settings.poll_seconds,
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    all_markets = ensure_markets(conn, settings)
    market_response = filtered_market_response(all_markets)
    return templates.TemplateResponse(
        request,
        "index.html",
        template_context(
            request,
            markets=market_response["markets"],
            market_meta=market_response,
            data_status_badge=data_status_badge(all_markets),
        ),
    )


@app.get("/markets/{market_id}", response_class=HTMLResponse)
async def market_detail(request: Request, market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    market = enrich_market_for_display(market)
    snapshots = list_snapshots(conn, market_id, limit=12)
    return templates.TemplateResponse(
        request,
        "market_detail.html",
        template_context(request, market=market, snapshots=snapshots),
    )


@app.get("/demo-positions", response_class=HTMLResponse)
async def demo_positions(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    return templates.TemplateResponse(
        request,
        "demo_positions.html",
        template_context(
            request,
            positions=list_positions(conn),
            orders=list_orders(conn),
            ledger=list_ledger(conn),
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
    markets = ensure_markets(conn, settings)
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


@app.get("/api/demo/balance")
async def api_demo_balance(conn: sqlite3.Connection = Depends(get_conn)):
    return {"user_id": DEMO_USER_ID, "balance": get_balance(conn)}


@app.get("/api/demo/positions")
async def api_demo_positions(conn: sqlite3.Connection = Depends(get_conn)):
    return {
        "balance": get_balance(conn),
        "positions": list_positions(conn),
        "orders": list_orders(conn),
        "ledger": list_ledger(conn),
    }


@app.post("/api/demo/predict")
async def api_demo_predict(payload: PredictionRequest, conn: sqlite3.Connection = Depends(get_conn)):
    try:
        result = create_demo_prediction(
            conn,
            market_id=payload.market_id,
            outcome=payload.outcome,
            stake=payload.stake,
        )
    except DemoPredictionError as exc:
        return JSONResponse(status_code=exc.status_code, content={"detail": str(exc)})
    return result
