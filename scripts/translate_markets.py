from __future__ import annotations

import argparse
import json
from pathlib import Path
import sqlite3
import sys
import time
from typing import Any, Sequence
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import Settings, get_settings
from app.storage import connect, init_db, list_markets_for_translation, record_translation_evaluation
from app.translation import (
    TRANSLATION_SUCCESS,
    TranslationPayload,
    TranslationUnavailableError,
    Translator,
    get_market_translation,
    get_translator,
    market_source_hashes,
    normalize_source_text,
    translation_quality_issues,
    translation_source_matches,
)

_SUPPORTED_PROVIDERS = {"azure", "local_marian", "noop"}
_DEFAULT_LIMIT_MAX = 50


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an integer") from exc
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be positive")
    return parsed


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate and cache guarded market translations.")
    parser.add_argument("--language", default=None)
    parser.add_argument("--limit", type=_positive_int, default=None)
    parser.add_argument("--market-id")
    parser.add_argument("--include-description", action="store_true", help="Retained for CLI compatibility; descriptions are evaluated by default.")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--retry-failed", action="store_true", help="Retained for CLI compatibility.")
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--fail-on-error", action="store_true")
    return parser


def _bounded_limit(value: int) -> int:
    return min(max(1, value), _DEFAULT_LIMIT_MAX)


def _provider_metadata(settings: Settings) -> tuple[str, str]:
    if settings.translation_provider == "azure":
        return "azure", f"azure-translator-{settings.azure_translator_api_version}"
    if settings.translation_provider == "local_marian":
        return "local_marian", settings.translation_model
    return "noop", "none"


def _configuration_error(settings: Settings, language: str, *, dry_run: bool) -> str | None:
    if settings.translation_provider not in _SUPPORTED_PROVIDERS:
        return "translation provider is not supported"
    expected_language = settings.azure_translator_target_language if settings.translation_provider == "azure" else settings.translation_target_language
    if language != expected_language:
        return "language does not match the configured translation target language"
    if settings.translation_provider == "azure":
        parsed = urlparse(settings.azure_translator_endpoint)
        if not parsed.scheme or not parsed.netloc:
            return "Azure Translator endpoint is invalid"
        if not dry_run and not settings.azure_translator_key:
            return "Azure Translator key is not configured"
    return None


def _safe_error_code(exc: Exception) -> str:
    if isinstance(exc, TranslationUnavailableError):
        return "provider_error"
    return "translation_error"


def _is_authentication_error(exc: Exception | None) -> bool:
    return bool(exc and ("HTTP 401" in str(exc) or "HTTP 403" in str(exc)))


def _market_fields(market: dict[str, Any]) -> dict[str, str]:
    return {
        "title": normalize_source_text(market.get("title")),
        "question": normalize_source_text(market.get("question")),
        "description": normalize_source_text(market.get("description")),
    }


def _cache_is_current(row: dict[str, Any] | None, market: dict[str, Any], language: str, provider: str, model: str) -> bool:
    return bool(
        row
        and row.get("translation_status") == TRANSLATION_SUCCESS
        and row.get("language") == language
        and row.get("translation_provider") == provider
        and row.get("translation_model") == model
        and translation_source_matches(row, market)
    )


def _quality_result(fields: dict[str, str], payload: TranslationPayload) -> tuple[str, list[str], dict[str, Any]]:
    translated = {
        "title": payload.title,
        "question": payload.question,
        "description": payload.description or "",
    }
    details: dict[str, Any] = {"fields": {}, "metered_characters_kind": "logical_input_characters"}
    failure_codes: list[str] = []
    translated_count = 0
    for field, source in fields.items():
        if not source:
            details["fields"][field] = {"status": "skipped", "failure_codes": []}
            continue
        translated_count += 1
        issues = translation_quality_issues(source, translated[field])
        field_codes = [f"{field}:{issue}" for issue in issues]
        details["fields"][field] = {"status": "failed" if field_codes else "passed", "failure_codes": issues}
        failure_codes.extend(field_codes)
    if translated_count == 0:
        raise ValueError("market has no translatable fields")
    return ("failed" if failure_codes else "passed"), failure_codes, details


def _failed_details(fields: dict[str, str], code: str) -> tuple[list[str], dict[str, Any]]:
    details: dict[str, Any] = {"fields": {}, "metered_characters_kind": "logical_input_characters"}
    failures: list[str] = []
    for field, source in fields.items():
        if not source:
            details["fields"][field] = {"status": "skipped", "failure_codes": []}
            continue
        details["fields"][field] = {"status": "failed", "failure_codes": [code]}
        failures.append(f"{field}:{code}")
    return failures, details


def _payload_for_market(payload: TranslationPayload) -> dict[str, str | None]:
    return {
        "translated_title": payload.title or None,
        "translated_question": payload.question or None,
        "translated_description": payload.description or None,
    }


def _emit(summary: dict[str, Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
        return
    print(
        "translation summary: "
        f"provider={summary['provider']} model={summary['model']} target_language={summary['target_language']} "
        f"selected={summary['selected']} translated={summary['translated']} passed={summary['passed']} "
        f"failed={summary['failed']} skipped_cached={summary['skipped_cached']} "
        f"skipped_empty={summary['skipped_empty']} attempts_recorded={summary['attempts_recorded']} "
        f"cache_updates={summary['cache_updates']} elapsed_ms={summary['elapsed_ms']}"
    )
    for item in summary["markets"]:
        print(
            f"market_id={item['market_id']} status={item['status']} attempt_id={item.get('attempt_id')} "
            f"cache_updated={item['cache_updated']} failure_codes={','.join(item['failure_codes'])}"
        )


def run(
    argv: Sequence[str] | None = None,
    *,
    conn: sqlite3.Connection | None = None,
    settings: Settings | None = None,
    translator: Translator | None = None,
) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    settings = settings or get_settings()
    language = (args.language or settings.translation_target_language or "ja").strip().lower()
    configuration_error = _configuration_error(settings, language, dry_run=args.dry_run)
    if configuration_error:
        print(configuration_error, file=sys.stderr)
        return 2
    provider, model = _provider_metadata(settings)
    limit = _bounded_limit(args.limit if args.limit is not None else settings.limit)
    started = time.monotonic()
    summary: dict[str, Any] = {
        "provider": provider,
        "model": model,
        "target_language": language,
        "selected": 0,
        "translated": 0,
        "passed": 0,
        "failed": 0,
        "skipped_cached": 0,
        "skipped_empty": 0,
        "attempts_recorded": 0,
        "cache_updates": 0,
        "error_counts": {},
        "failure_counts": {},
        "markets": [],
        "elapsed_ms": 0,
    }
    owns_connection = conn is None
    try:
        conn = conn or connect(settings.db_path)
        init_db(conn)
        if not args.dry_run:
            translator = translator or get_translator(settings)
            provider = getattr(translator, "provider", provider)
            model = getattr(translator, "model", model)
            summary["provider"] = provider
            summary["model"] = model
        markets = list_markets_for_translation(conn, limit, args.market_id)
        if args.market_id and not markets:
            print("market was not found", file=sys.stderr)
            return 2
        selected: list[tuple[dict[str, Any], dict[str, str]]] = []
        for market in markets:
            fields = _market_fields(market)
            if not any(fields.values()):
                summary["skipped_empty"] += 1
                summary["markets"].append({"market_id": market["market_id"], "status": "skipped_empty", "attempt_id": None, "cache_updated": False, "failure_codes": []})
                continue
            cached = get_market_translation(conn, market["market_id"], language)
            if not args.force and _cache_is_current(cached, market, language, provider, model):
                summary["skipped_cached"] += 1
                summary["markets"].append({"market_id": market["market_id"], "status": "skipped_cached", "attempt_id": None, "cache_updated": False, "failure_codes": []})
                continue
            selected.append((market, fields))
        summary["selected"] = len(selected)
        if args.dry_run:
            for market, _ in selected:
                summary["markets"].append({"market_id": market["market_id"], "status": "dry_run", "attempt_id": None, "cache_updated": False, "failure_codes": []})
            return 0

        payloads: list[TranslationPayload] | None = None
        call_error: Exception | None = None
        call_started = time.monotonic()
        try:
            requests = [(fields["title"], fields["question"], fields["description"], language) for _, fields in selected]
            batch = getattr(translator, "translate_batch", None)
            if callable(batch):
                payloads = batch(requests)
            else:
                payloads = [translator.translate(title=title, question=question, description=description, target_language=target) for title, question, description, target in requests]
            if len(payloads) != len(selected):
                raise TranslationUnavailableError("translator returned an incomplete batch")
        except Exception as exc:
            call_error = exc
        latency_ms = max(0, int((time.monotonic() - call_started) * 1000))
        for index, (market, fields) in enumerate(selected):
            metered_characters = sum(len(value) for value in fields.values()) if call_error is None else 0
            if call_error is not None:
                code = _safe_error_code(call_error)
                failure_codes, quality_details = _failed_details(fields, code)
                translated_fields = {"translated_title": None, "translated_question": None, "translated_description": None}
                status = "failed"
                summary["error_counts"][code] = summary["error_counts"].get(code, 0) + 1
            else:
                assert payloads is not None
                payload = payloads[index]
                translated_fields = _payload_for_market(payload)
                status, failure_codes, quality_details = _quality_result(fields, payload)
                metered_characters = sum(len(value) for value in fields.values())
            try:
                recorded = record_translation_evaluation(
                    conn,
                    market_id=market["market_id"],
                    target_language=language,
                    translation_provider=provider,
                    translation_model=model,
                    **market_source_hashes(market),
                    **translated_fields,
                    quality_status=status,
                    quality_failure_codes=failure_codes,
                    quality_details=quality_details,
                    latency_ms=latency_ms,
                    metered_characters=metered_characters,
                    provider_request_id=None,
                )
            except Exception:
                print("translation audit storage failed", file=sys.stderr)
                return 2
            summary["translated"] += 1
            summary["attempts_recorded"] += 1
            summary[status] += 1
            if recorded["accepted"]:
                summary["cache_updates"] += 1
            for code in failure_codes:
                summary["failure_counts"][code] = summary["failure_counts"].get(code, 0) + 1
            summary["markets"].append({
                "market_id": market["market_id"],
                "status": status,
                "attempt_id": recorded["attempt"]["id"],
                "cache_updated": recorded["accepted"],
                "failure_codes": failure_codes,
            })
        if _is_authentication_error(call_error):
            return 2
        return 1 if args.fail_on_error and summary["failed"] else 0
    except sqlite3.Error:
        print("translation database operation failed", file=sys.stderr)
        return 2
    finally:
        summary["elapsed_ms"] = max(0, int((time.monotonic() - started) * 1000))
        _emit(summary, as_json=args.json)
        if owns_connection and conn is not None:
            conn.close()


def main() -> int:
    return run()


if __name__ == "__main__":
    raise SystemExit(main())
