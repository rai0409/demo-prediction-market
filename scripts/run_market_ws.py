from __future__ import annotations

import asyncio
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.polymarket_ws import extract_asset_ids_from_market, run_market_ws_once, select_ws_markets
from app.realtime import ensure_fresh_markets
from app.storage import connect, init_db


async def main() -> int:
    settings = get_settings()
    conn = connect(settings.db_path)
    init_db(conn)
    markets = ensure_fresh_markets(conn, settings)
    selected = select_ws_markets(markets, settings.ws_top_n)
    asset_count = sum(len(extract_asset_ids_from_market(market)) for market in selected)
    print(f"ws_enabled={settings.ws_enabled}")
    print(f"selected_market_count={len(selected)}")
    print(f"asset_id_count={asset_count}")
    if not settings.ws_enabled:
        print("WebSocket mode is disabled. Set DEMO_PREDICTION_WS_ENABLED=1 to run.")
        return 0
    inserted = await run_market_ws_once(conn, markets, top_n=settings.ws_top_n)
    print(f"inserted_update_count={inserted}")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
