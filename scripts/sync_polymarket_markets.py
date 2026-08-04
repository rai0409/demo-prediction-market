from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Sequence

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings
from app.polymarket_sync import MAX_SYNC_LIMIT, sync_polymarket_markets
from app.storage import connect, init_db


def _positive_limit(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("limit must be an integer") from exc
    if not 1 <= parsed <= MAX_SYNC_LIMIT:
        raise argparse.ArgumentTypeError(f"limit must be between 1 and {MAX_SYNC_LIMIT}")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one read-only Polymarket public market-data synchronization.")
    parser.add_argument("--limit", type=_positive_limit, default=None)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--json", action="store_true")
    return parser


def exit_code(summary: dict) -> int:
    if summary["status"] in {"success", "dry_run"}:
        return 0
    if summary["status"] == "partial_success":
        return 1
    return 2


def run(argv: Sequence[str] | None = None, *, conn=None, settings: Settings | None = None, fetcher=None) -> tuple[int, dict]:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    owns_connection = conn is None
    conn = conn or connect(settings.db_path)
    try:
        init_db(conn)
        kwargs = {"limit": args.limit, "dry_run": args.dry_run}
        if fetcher is not None:
            kwargs["fetcher"] = fetcher
        summary = sync_polymarket_markets(conn, settings, **kwargs)
        return exit_code(summary), summary
    finally:
        if owns_connection:
            conn.close()


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    code, summary = run(argv)
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        print(
            " ".join(
                f"{key}={summary[key]}"
                for key in ("status", "requested", "received", "valid", "inserted", "updated", "unchanged", "skipped", "failed", "elapsed_ms", "dry_run")
            )
        )
    return code


if __name__ == "__main__":
    raise SystemExit(main())
