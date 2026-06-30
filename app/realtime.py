from __future__ import annotations

import sqlite3

from app.config import Settings
from app.polymarket_gamma import load_markets
from app.storage import list_markets, store_markets


def refresh_markets(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    markets = load_markets(live=settings.live, limit=settings.limit)
    store_markets(conn, markets)
    return markets


def ensure_markets(conn: sqlite3.Connection, settings: Settings) -> list[dict]:
    markets = list_markets(conn)
    if markets:
        return markets
    return refresh_markets(conn, settings)
