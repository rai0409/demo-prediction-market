from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from app.translation import LocalMarianTranslator, TranslationUnavailableError, translation_quality_issues


DEFAULT_FIXTURE = Path("tests/fixtures/translation_quality_cases.json")


def evaluate_case(case: dict, translated: str) -> list[str]:
    source = case["source"]
    issues: list[str] = []
    issues.extend(translation_quality_issues(source, translated))
    if len(translated.strip()) < max(2, len(source.strip()) // 10) or len(translated) > len(source) * 5:
        issues.append("length")
    for number in case.get("required_numbers", []):
        if number not in translated:
            issues.append(f"number:{number}")
    for token in case.get("required_tokens", []):
        if token not in translated:
            issues.append(f"token:{token}")
    if "%" in source and "%" not in translated:
        issues.append("percent")
    return issues


def run(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Evaluate offline translation condition-preservation checks.")
    parser.add_argument("--fixture", default=str(DEFAULT_FIXTURE))
    parser.add_argument("--model-id", default="Helsinki-NLP/opus-mt-en-jap")
    parser.add_argument("--device", default="auto")
    parser.add_argument("--local-files-only", action="store_true")
    args = parser.parse_args(argv)
    cases = json.loads(Path(args.fixture).read_text(encoding="utf-8"))
    translator = LocalMarianTranslator(
        model_id=args.model_id,
        device=args.device,
        local_files_only=args.local_files_only,
    )
    try:
        outputs = translator.translate_batch([(case["source"], case["source"], "", "ja") for case in cases])
    except TranslationUnavailableError as exc:
        print(f"quality evaluation unavailable: {exc}", file=sys.stderr)
        return 2
    failures = 0
    for case, output in zip(cases, outputs):
        translated = output.title
        issues = evaluate_case(case, translated)
        print(f"{case['id']}: {translated}")
        if issues:
            failures += 1
            print(f"  gate_failed={','.join(issues)}", file=sys.stderr)
    summary = translator.summary()
    print(f"quality summary: cases={len(cases)} failed={failures} device={summary['device']} cpu_fallback={summary['cpu_fallback']}")
    return 1 if failures else 0


if __name__ == "__main__":
    raise SystemExit(run())
