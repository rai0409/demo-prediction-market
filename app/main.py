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
from app.realtime import ensure_markets, refresh_markets
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


@app.get("/", response_class=HTMLResponse)
async def index(request: Request, conn: sqlite3.Connection = Depends(get_conn)):
    markets = ensure_markets(conn, settings)
    return templates.TemplateResponse(request, "index.html", template_context(request, markets=markets))


@app.get("/markets/{market_id}", response_class=HTMLResponse)
async def market_detail(request: Request, market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
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
async def api_markets(conn: sqlite3.Connection = Depends(get_conn)):
    markets = ensure_markets(conn, settings)
    return {"markets": markets, "count": len(markets)}


@app.get("/api/markets/{market_id}")
async def api_market(market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    ensure_markets(conn, settings)
    market = get_market(conn, market_id)
    if market is None:
        raise HTTPException(status_code=404, detail="market not found")
    return market


@app.get("/api/markets/{market_id}/snapshots")
async def api_snapshots(market_id: str, conn: sqlite3.Connection = Depends(get_conn)):
    return {"snapshots": list_snapshots(conn, market_id)}


@app.post("/api/refresh")
async def api_refresh(conn: sqlite3.Connection = Depends(get_conn)):
    markets = refresh_markets(conn, settings)
    return {"markets": markets, "count": len(markets)}


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
