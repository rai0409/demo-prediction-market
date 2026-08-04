from __future__ import annotations

import importlib.util
import io
import json
from pathlib import Path
from contextlib import redirect_stdout
import subprocess
import sys
import time

import httpx
import pytest

from app.config import Settings
from app.polymarket_gamma import FetchResult, fetch_live_markets, gamma_events_url
from app.polymarket_sync import sync_polymarket_markets
from app.storage import (
    get_last_successful_market_sync_run,
    get_latest_market_sync_run,
    get_market,
    init_db,
    list_snapshots,
    connect,
)


def _script_module():
    path = Path("scripts/sync_polymarket_markets.py")
    spec = importlib.util.spec_from_file_location("sync_polymarket_markets", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _market(market_id: str = "sync-market", *, title: str = "One-shot market", description: str = "raw private source description"):
    return {
        "market_id": market_id,
        "source": "polymarket",
        "title": title,
        "question": "Will the one-shot sync remain safe?",
        "description": description,
        "outcomes": ["YES", "NO"],
        "probabilities": {"YES": 0.4, "NO": 0.6},
        "volume": 100.0,
        "volume_24hr": 10.0,
        "liquidity": 50.0,
        "active": True,
        "closed": False,
        "resolved": False,
        "end_date": "2099-01-01T00:00:00+00:00",
        "fetched_at": "2026-01-01T00:00:00+00:00",
        "data_source_status": "live",
    }


def _result(markets, *, ok=True, status="live", raw_count=None, attempted_at="2026-01-01T00:00:00+00:00", error=None):
    return FetchResult(
        ok=ok,
        status=status,
        error=error,
        raw_count=len(markets) if raw_count is None else raw_count,
        normalized_count=len(markets),
        markets=markets,
        attempted_at=attempted_at,
        url="https://gamma-api.polymarket.com/events?secret=not-output",
        http_status=200 if ok else None,
    )


def _settings():
    return Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:")


def test_one_shot_sync_inserts_then_is_idempotent_and_reports_structured_summary(db_conn):
    market = _market()
    first = sync_polymarket_markets(db_conn, _settings(), fetcher=lambda **_: _result([market]))
    second = sync_polymarket_markets(db_conn, _settings(), fetcher=lambda **_: _result([market]))

    assert first["status"] == "success"
    assert first["inserted"] == 1
    assert first["updated"] == first["unchanged"] == 0
    assert second["status"] == "success"
    assert second["unchanged"] == 1
    assert second["inserted"] == second["updated"] == 0
    assert db_conn.execute("select count(*) from markets where market_id = ?", (market["market_id"],)).fetchone()[0] == 1
    assert len(list_snapshots(db_conn, market["market_id"])) == 1
    assert {"requested", "received", "valid", "inserted", "updated", "unchanged", "skipped", "failed", "elapsed_ms", "retrieved_at", "provider", "dry_run", "error_counts"}.issubset(first)


def test_one_shot_sync_updates_changed_payload_without_losing_raw_fields(db_conn):
    initial = _market(description="original raw description")
    changed = _market(title="Changed title", description="changed raw description")
    sync_polymarket_markets(db_conn, _settings(), fetcher=lambda **_: _result([initial]))
    summary = sync_polymarket_markets(db_conn, _settings(), fetcher=lambda **_: _result([changed], attempted_at="2026-01-02T00:00:00+00:00"))

    assert summary["updated"] == 1
    assert summary["source_changed"] == 1
    assert get_market(db_conn, initial["market_id"])["description"] == "changed raw description"
    assert len(list_snapshots(db_conn, initial["market_id"])) == 2


def test_one_shot_sync_dry_run_calculates_changes_without_writing(db_conn):
    summary = sync_polymarket_markets(db_conn, _settings(), dry_run=True, fetcher=lambda **_: _result([_market("dry-run-market")]))

    assert summary["status"] == "dry_run"
    assert summary["inserted"] == 1
    assert get_market(db_conn, "dry-run-market") is None
    assert get_latest_market_sync_run(db_conn) is None


def test_one_shot_sync_preserves_existing_markets_for_upstream_failures(db_conn):
    existing = _market("preserved-market")
    sync_polymarket_markets(db_conn, _settings(), fetcher=lambda **_: _result([existing]))
    previous_success = get_last_successful_market_sync_run(db_conn)["successful_at"]

    for status in ("timeout", "rate_limited", "upstream_4xx", "upstream_5xx", "invalid_response", "live_empty"):
        summary = sync_polymarket_markets(
            db_conn,
            _settings(),
            fetcher=lambda _status=status, **_: _result([], ok=False, status=_status, error="Authorization: Bearer secret-token"),
        )
        assert summary["status"] in {"timeout", "rate_limited", "upstream_4xx", "upstream_5xx", "invalid_response", "empty_response"}
        assert get_market(db_conn, "preserved-market") is not None
        assert get_last_successful_market_sync_run(db_conn)["successful_at"] == previous_success
        assert get_latest_market_sync_run(db_conn)["error_code"] == summary["error_code"]
        assert "secret-token" not in json.dumps(summary)


def test_one_shot_sync_keeps_valid_records_when_some_records_are_invalid(db_conn):
    summary = sync_polymarket_markets(
        db_conn,
        _settings(),
        fetcher=lambda **_: _result([_market("valid-market"), {"market_id": ""}, _market("valid-market")]),
    )

    assert summary["status"] == "partial_success"
    assert summary["inserted"] == 1
    assert summary["failed"] == 2
    assert summary["error_counts"] == {"duplicate_market_id": 1, "invalid_record": 1}
    assert get_market(db_conn, "valid-market") is not None


def test_one_shot_sync_rolls_back_on_storage_error(db_conn):
    db_conn.execute(
        """create trigger fail_sync_snapshot before insert on market_snapshots
        begin select raise(abort, 'snapshot blocked'); end"""
    )
    db_conn.commit()

    summary = sync_polymarket_markets(db_conn, _settings(), fetcher=lambda **_: _result([_market("rollback-market")]))

    assert summary["status"] == "storage_error"
    assert get_market(db_conn, "rollback-market") is None


def test_one_shot_cli_json_and_two_runs_use_mocked_fetcher(db_conn, monkeypatch):
    module = _script_module()
    fetcher = lambda **_: _result([_market("cli-market")])
    code, first = module.run(["--json"], conn=db_conn, settings=_settings(), fetcher=fetcher)
    second_code, second = module.run(["--dry-run", "--json"], conn=db_conn, settings=_settings(), fetcher=fetcher)
    output = io.StringIO()
    monkeypatch.setattr(module, "run", lambda _argv: (0, first))
    with redirect_stdout(output):
        assert module.main(["--json"]) == 0

    assert code == 0
    assert first["inserted"] == 1
    assert second_code == 0
    assert second["unchanged"] == 1
    parsed = json.loads(output.getvalue())
    assert isinstance(parsed, dict)
    assert "markets" not in parsed


def test_gamma_events_url_is_read_only_active_open_and_respects_requested_limit():
    url = gamma_events_url(5)
    assert "active=true" in url
    assert "closed=false" in url
    assert "limit=5" in url
    assert "order=" not in url


@pytest.mark.parametrize(
    ("response_status", "expected"),
    [(400, "upstream_4xx"), (429, "rate_limited"), (503, "upstream_5xx")],
)
def test_gamma_fetch_classifies_mocked_http_statuses(monkeypatch, tmp_path, response_status, expected):
    import app.polymarket_gamma as gamma

    class Response:
        status_code = response_status

    monkeypatch.setattr(gamma, "GAMMA_ERROR_PATH", tmp_path / "error.txt")
    monkeypatch.setattr(gamma, "GAMMA_STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(gamma.httpx, "get", lambda *args, **kwargs: Response())

    result = fetch_live_markets(limit=1)
    assert result.ok is False
    assert result.status == expected


def test_gamma_fetch_classifies_mocked_timeout(monkeypatch, tmp_path):
    import app.polymarket_gamma as gamma

    monkeypatch.setattr(gamma, "GAMMA_ERROR_PATH", tmp_path / "error.txt")
    monkeypatch.setattr(gamma, "GAMMA_STATUS_PATH", tmp_path / "status.json")
    monkeypatch.setattr(gamma.httpx, "get", lambda *args, **kwargs: (_ for _ in ()).throw(httpx.TimeoutException("timed out")))

    result = fetch_live_markets(limit=1)
    assert result.ok is False
    assert result.status == "timeout"


def test_process_lock_rejects_second_sync_without_fetching_or_writing(tmp_path):
    db_path = tmp_path / "shared.sqlite"
    conn = connect(str(db_path))
    init_db(conn)
    settings = Settings(live=False, poll_seconds=30, limit=5, db_path=str(db_path))
    holder = subprocess.Popen(
        [sys.executable, "-c", "import fcntl,sys,time; f=open(sys.argv[1], 'a'); fcntl.flock(f, fcntl.LOCK_EX); print('locked', flush=True); time.sleep(5)", str(db_path.resolve()) + ".sync.lock"],
        stdout=subprocess.PIPE,
        text=True,
    )
    try:
        assert holder.stdout is not None and holder.stdout.readline().strip() == "locked"
        called = False
        def fetcher(**_):
            nonlocal called
            called = True
            return _result([_market("should-not-fetch")])
        before = conn.execute("select count(*) from market_sync_runs").fetchone()[0]
        summary = sync_polymarket_markets(conn, settings, dry_run=True, fetcher=fetcher)
        assert summary["status"] == summary["error_code"] == "sync_already_running"
        assert called is False
        assert conn.execute("select count(*) from market_sync_runs").fetchone()[0] == before
    finally:
        holder.terminate(); holder.wait(timeout=5); conn.close()
