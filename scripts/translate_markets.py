from __future__ import annotations

import argparse
import sqlite3
from typing import Sequence

from app.config import Settings, get_settings
from app.storage import connect, init_db, list_markets_for_translation
from app.translation import (
    TRANSLATION_FAILED,
    TRANSLATION_SUCCESS,
    TranslationUnavailableError,
    Translator,
    get_market_translation,
    get_translator,
    normalize_source_text,
    translation_source_matches,
    upsert_market_translation,
)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Store cached market translations without serving translations in web requests.")
    parser.add_argument("--language", default=None)
    parser.add_argument("--limit", type=int, default=20)
    parser.add_argument("--market-id")
    parser.add_argument("--include-description", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true")
    return parser


def _bounded_limit(value: int) -> int:
    return max(1, min(value, 50))


def run(
    argv: Sequence[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    translator: Translator | None = None,
) -> int:
    args = build_parser().parse_args(argv)
    settings = settings or get_settings()
    language = (args.language or settings.translation_target_language or "ja").strip().lower()
    limit = _bounded_limit(args.limit)
    owns_connection = conn is None
    conn = conn or connect(settings.db_path)
    init_db(conn)
    translator = translator or get_translator(settings)
    success = skipped = failed = pending = 0
    try:
        markets = list_markets_for_translation(conn, limit, args.market_id)
        for market in markets:
            cached = get_market_translation(conn, market["market_id"], language)
            cached_current = translation_source_matches(cached, market)
            if cached_current and cached and cached["translation_status"] == TRANSLATION_SUCCESS and not args.force:
                skipped += 1
                continue
            if cached_current and cached and cached["translation_status"] == TRANSLATION_FAILED and not (args.force or args.retry_failed):
                skipped += 1
                continue
            if args.dry_run:
                pending += 1
                continue
            title = normalize_source_text(market.get("title"))
            question = normalize_source_text(market.get("question"))
            description = normalize_source_text(market.get("description")) if args.include_description else ""
            if len(title) + len(question) + len(description) > settings.translation_max_chars:
                upsert_market_translation(
                    conn,
                    market=market,
                    language=language,
                    translated_title=None,
                    translated_question=None,
                    translated_description=None,
                    provider=getattr(translator, "provider", "unknown"),
                    model=getattr(translator, "model", "unknown"),
                    status=TRANSLATION_FAILED,
                    error_message="source exceeds configured translation length",
                )
                failed += 1
                continue
            try:
                payload = translator.translate(
                    title=title,
                    question=question,
                    description=description,
                    target_language=language,
                )
            except TranslationUnavailableError:
                upsert_market_translation(
                    conn,
                    market=market,
                    language=language,
                    translated_title=None,
                    translated_question=None,
                    translated_description=None,
                    provider=getattr(translator, "provider", "noop"),
                    model=getattr(translator, "model", "none"),
                    status=TRANSLATION_FAILED,
                    error_message="translation unavailable",
                )
                failed += 1
                continue
            except Exception:
                upsert_market_translation(
                    conn,
                    market=market,
                    language=language,
                    translated_title=None,
                    translated_question=None,
                    translated_description=None,
                    provider=getattr(translator, "provider", "unknown"),
                    model=getattr(translator, "model", "unknown"),
                    status=TRANSLATION_FAILED,
                    error_message="translation failed",
                )
                failed += 1
                continue
            upsert_market_translation(
                conn,
                market=market,
                language=language,
                translated_title=payload.title,
                translated_question=payload.question,
                translated_description=payload.description if args.include_description else None,
                provider=payload.provider,
                model=payload.model,
                status=TRANSLATION_SUCCESS,
            )
            success += 1
    finally:
        if owns_connection:
            conn.close()
    print(f"translation summary: success={success} skipped={skipped} failed={failed} dry_run={pending}")
    return 2 if failed else 0


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
