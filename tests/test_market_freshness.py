from datetime import datetime, timedelta, timezone

import pytest

from app.market_freshness import classify_market_freshness


NOW = datetime(2026, 1, 2, 12, 0, 0, tzinfo=timezone.utc)


@pytest.mark.parametrize(
    ("successful_at", "expected"),
    [
        (None, "unavailable"),
        (NOW, "current"),
        (NOW - timedelta(minutes=1, seconds=59, microseconds=999999), "current"),
        (NOW - timedelta(minutes=2), "delayed"),
        (NOW - timedelta(minutes=9, seconds=59, microseconds=999999), "delayed"),
        (NOW - timedelta(minutes=10), "stale"),
        (NOW - timedelta(hours=23, minutes=59, seconds=59, microseconds=999999), "stale"),
        (NOW - timedelta(hours=24), "unavailable"),
        (NOW - timedelta(hours=25), "unavailable"),
        (NOW + timedelta(seconds=1), "unavailable"),
    ],
)
def test_market_freshness_boundaries(successful_at, expected):
    assert classify_market_freshness(successful_at, now=NOW)["freshness_status"] == expected


def test_market_freshness_normalizes_naive_and_aware_datetimes_to_utc():
    naive = datetime(2026, 1, 2, 11, 59)
    aware = datetime(2026, 1, 2, 20, 59, tzinfo=timezone(timedelta(hours=9)))

    assert classify_market_freshness(naive, now=NOW)["freshness_status"] == "current"
    assert classify_market_freshness(aware, now=NOW)["freshness_status"] == "current"
    assert classify_market_freshness(naive, now=NOW)["last_sync_success_at"] == "2026-01-02T11:59:00+00:00"


def test_market_freshness_hides_future_success_timestamp():
    result = classify_market_freshness(NOW + timedelta(seconds=1), now=NOW)
    assert result == {"freshness_status": "unavailable", "last_sync_success_at": None}
