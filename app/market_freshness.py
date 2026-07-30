from __future__ import annotations

from datetime import datetime, timezone
from typing import Any


CURRENT_SECONDS = 2 * 60
DELAYED_SECONDS = 10 * 60
STALE_SECONDS = 24 * 60 * 60


def _utc_datetime(value: datetime | str | None) -> datetime | None:
    if isinstance(value, str):
        try:
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    if not isinstance(value, datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def classify_market_freshness(
    last_sync_success_at: datetime | str | None,
    *,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Classify external market-data age without performing I/O or mutation."""
    current = _utc_datetime(now or datetime.now(timezone.utc))
    successful_at = _utc_datetime(last_sync_success_at)
    normalized_success_at = successful_at.isoformat() if successful_at else None
    if current is None or successful_at is None:
        return {"freshness_status": "unavailable", "last_sync_success_at": normalized_success_at}

    age_seconds = (current - successful_at).total_seconds()
    if age_seconds < 0:
        return {"freshness_status": "unavailable", "last_sync_success_at": None}
    if age_seconds >= STALE_SECONDS:
        return {"freshness_status": "unavailable", "last_sync_success_at": normalized_success_at}
    if age_seconds < CURRENT_SECONDS:
        status = "current"
    elif age_seconds < DELAYED_SECONDS:
        status = "delayed"
    else:
        status = "stale"
    return {"freshness_status": status, "last_sync_success_at": normalized_success_at}
