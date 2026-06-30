from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx

from app.polymarket_gamma import gamma_events_url


def main() -> int:
    runtime = ROOT / "runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    url = gamma_events_url(100)
    headers = {
        "Accept": "application/json",
        "User-Agent": "DemoPredictionMarketViewer/0.2 (+diagnostics; no-trading)",
    }
    try:
        response = httpx.get(url, headers=headers, timeout=8.0)
        print(f"http_status={response.status_code}")
        response.raise_for_status()
        payload = response.json()
    except Exception as exc:
        print(f"error={exc}")
        return 1

    output_path = runtime / "manual_gamma_response.json"
    output_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"wrote={output_path}")
    print(f"top_level_type={type(payload).__name__}")
    if isinstance(payload, list):
        print(f"top_level_count={len(payload)}")
        first_event = payload[0] if payload else {}
    elif isinstance(payload, dict):
        print(f"top_level_keys={sorted(payload.keys())}")
        items = payload.get("events") or payload.get("data") or []
        print(f"top_level_count={len(items) if isinstance(items, list) else 1}")
        first_event = items[0] if isinstance(items, list) and items else payload
    else:
        first_event = {}
    if isinstance(first_event, dict):
        print(f"first_event_keys={sorted(first_event.keys())}")
        markets = first_event.get("markets")
        if isinstance(markets, list) and markets and isinstance(markets[0], dict):
            print(f"first_market_keys={sorted(markets[0].keys())}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
