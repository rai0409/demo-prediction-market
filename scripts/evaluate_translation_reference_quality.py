from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.config import get_settings
from app.translation import TranslationUnavailableError, get_translator
from app.translation_reference_evaluation import (
    FixtureValidationError,
    azure_translations,
    evaluate_cases,
    load_fixture,
    select_cases,
)


DEFAULT_FIXTURE = "tests/fixtures/translation_reference_quality_cases.json"


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Evaluate reference translations with the shared quality gate.")
    parser.add_argument("--provider", default="fixture", choices=("fixture", "azure"))
    parser.add_argument("--fixture", default=DEFAULT_FIXTURE)
    parser.add_argument("--case-id")
    parser.add_argument("--category")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--json", action="store_true")
    parser.add_argument("--include-text", action="store_true", help="Include source, reference, and candidate text in JSON diagnostics.")
    parser.add_argument("--fail-on-quality-error", action="store_true")
    return parser


def _print_summary(summary: dict[str, Any]) -> None:
    print(f"provider={summary['provider']}")
    print(f"total={summary['total']} passed={summary['passed']} failed={summary['failed']} pass_rate={summary['pass_rate']:.3f}")
    print(f"failure_counts={json.dumps(summary['failure_counts'], ensure_ascii=False, sort_keys=True)}")
    print(f"category_counts={json.dumps(summary['category_counts'], ensure_ascii=False, sort_keys=True)}")
    for case in summary["cases"]:
        if case["status"] == "failed":
            print(f"failed_case={case['id']} failure_codes={','.join(case['failure_codes'])}")


def run(argv: list[str] | None = None, *, translator: Any | None = None) -> int:
    try:
        args = build_parser().parse_args(argv)
    except SystemExit as exc:
        return int(exc.code)
    if args.limit is not None and args.limit < 1:
        print("invalid limit", file=sys.stderr)
        return 2
    try:
        cases = select_cases(load_fixture(args.fixture), args.case_id, args.category, args.limit)
        if args.provider == "fixture":
            translations = [case["candidate_translation"] for case in cases]
        else:
            settings = get_settings()
            if not settings.azure_translator_key:
                raise FixtureValidationError("Azure Translator key is not configured")
            translations = azure_translations(translator or get_translator(settings), cases, settings.azure_translator_target_language)
        summary = evaluate_cases(cases, translations)
    except (FixtureValidationError, TranslationUnavailableError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    summary["provider"] = args.provider
    if args.include_text:
        for result, case, candidate in zip(summary["cases"], cases, translations):
            result["source"] = case["source"]
            result["reference_translation"] = case["reference_translation"]
            result["candidate_translation"] = candidate
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, sort_keys=True))
    else:
        _print_summary(summary)
    return 1 if args.fail_on_quality_error and summary["failed"] else 0


if __name__ == "__main__":
    raise SystemExit(run())
