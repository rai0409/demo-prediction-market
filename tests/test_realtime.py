from app.config import Settings
from app.polymarket_gamma import FetchResult, utc_now_iso
from app.realtime import refresh_markets_with_result
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
