from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.polymarket_gamma import fetch_live_markets, sample_fetch_result


def main() -> int:
    settings = get_settings()
    print(f"live_enabled={settings.live}")
    if settings.live:
        result = fetch_live_markets(limit=settings.limit)
        if not result.ok:
            print(f"fetch_status={result.status}")
            print(f"error={result.error}")
            fallback = sample_fetch_result(
                limit=settings.limit,
                status="live_failed_sample_fallback",
                error=result.error,
                live_enabled=True,
            )
            print(f"fallback_status={fallback.status}")
            print(f"normalized_market_count={fallback.normalized_count}")
            for market in fallback.markets[:5]:
                print(f"- {market['title']}")
            return 0 if fallback.ok else 1
    else:
        result = sample_fetch_result(limit=settings.limit, status="sample_fallback", live_enabled=False)

    print(f"fetch_status={result.status}")
    if result.error:
        print(f"error={result.error}")
    print(f"normalized_market_count={result.normalized_count}")
    for market in result.markets[:5]:
        print(f"- {market['title']}")
    return 0 if result.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
