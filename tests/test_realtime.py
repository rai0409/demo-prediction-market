from datetime import datetime, timedelta, timezone

from app.config import Settings
from app.polymarket_gamma import FetchResult, utc_now_iso
from app.realtime import ensure_fresh_markets, refresh_markets_with_result
from app.storage import list_markets


def test_refresh_live_failure_falls_back_to_sample(db_conn, monkeypatch):
    def fake_fetch_live_markets(limit):
        return FetchResult(
            ok=False,
            status="live_failed",
            error="network unavailable",
            raw_count=0,
            normalized_count=0,
            markets=[],
            attempted_at=utc_now_iso(),
            url="https://example.invalid",
            http_status=None,
        )

    monkeypatch.setattr("app.realtime.fetch_live_markets", fake_fetch_live_markets)
    settings = Settings(live=True, poll_seconds=30, limit=50, db_path=":memory:")
    result = refresh_markets_with_result(db_conn, settings)
    assert result["status"] == "live_failed_sample_fallback"
    assert result["fallback_used"] is True
    assert result["error"] == "network unavailable"
    assert list_markets(db_conn)[0]["source"] == "sample"


def test_ensure_fresh_markets_does_not_refresh_within_window(db_conn, monkeypatch):
    def fail_refresh(conn, settings):
        raise AssertionError("refresh should not run")

    monkeypatch.setattr("app.realtime.refresh_markets", fail_refresh)
    settings = Settings(
        live=False,
        poll_seconds=30,
        limit=50,
        db_path=":memory:",
        auto_refresh=True,
        refresh_seconds=300,
    )
    assert len(ensure_fresh_markets(db_conn, settings)) >= 3


def test_ensure_fresh_markets_refreshes_when_stale(db_conn, monkeypatch, sample_markets):
    stale = (datetime.now(timezone.utc) - timedelta(seconds=600)).isoformat()
    db_conn.execute("update fetch_runs set fetched_at = ?", (stale,))
    db_conn.commit()
    refreshed = [dict(sample_markets[0], market_id="refreshed-market")]

    def fake_refresh(conn, settings):
        return refreshed

    monkeypatch.setattr("app.realtime.refresh_markets", fake_refresh)
    settings = Settings(
        live=False,
        poll_seconds=30,
        limit=50,
        db_path=":memory:",
        auto_refresh=True,
        refresh_seconds=15,
    )
    assert ensure_fresh_markets(db_conn, settings) == refreshed
