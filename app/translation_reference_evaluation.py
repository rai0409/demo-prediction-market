from __future__ import annotations

import json
from pathlib import Path
import re
import unicodedata
from typing import Any

from app.translation import TranslationPayload, translation_quality_issues


REQUIRED_CASE_FIELDS = {
    "id": str,
    "category": str,
    "source": str,
    "reference_translation": str,
    "accepted_translations": list,
    "required_numbers": list,
    "required_terms": list,
    "protected_terms": dict,
    "expected_logic_operators": list,
    "expected_quality_status": str,
    "notes": str,
}
QUALITY_STATUSES = {"passed", "failed"}


class FixtureValidationError(ValueError):
    """Raised when a reference quality fixture cannot be evaluated safely."""


def load_fixture(path: str | Path) -> list[dict[str, Any]]:
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise FixtureValidationError("fixture could not be read") from exc
    if not isinstance(payload, list):
        raise FixtureValidationError("fixture root must be a list")
    seen_ids: set[str] = set()
    for case in payload:
        if not isinstance(case, dict):
            raise FixtureValidationError("fixture case must be an object")
        for field, expected_type in REQUIRED_CASE_FIELDS.items():
            value = case.get(field)
            if not isinstance(value, expected_type) or (expected_type is str and not value.strip()):
                raise FixtureValidationError(f"fixture case has invalid {field}")
        if case["id"] in seen_ids:
            raise FixtureValidationError("fixture has duplicate id")
        seen_ids.add(case["id"])
        if case["expected_quality_status"] not in QUALITY_STATUSES:
            raise FixtureValidationError("fixture has unsupported expected_quality_status")
        for field in ("accepted_translations", "required_numbers", "required_terms", "expected_logic_operators"):
            if not all(isinstance(value, str) for value in case[field]):
                raise FixtureValidationError(f"fixture case has invalid {field}")
        if not all(
            isinstance(key, str) and isinstance(values, list) and all(isinstance(value, str) for value in values)
            for key, values in case["protected_terms"].items()
        ):
            raise FixtureValidationError("fixture case has invalid protected_terms")
        if not isinstance(case.get("candidate_translation"), str) or not case["candidate_translation"].strip():
            raise FixtureValidationError("fixture case is missing evaluation translation")
    return payload


def _normalized(value: str) -> str:
    return unicodedata.normalize("NFKC", value).replace(",", "").replace("，", "")


def _number_present(number: str, translated: str) -> bool:
    normalized_number = _normalized(number)
    normalized_translation = _normalized(translated)
    if normalized_number in normalized_translation:
        return True
    if normalized_number.isdigit() and int(normalized_number) % 10000 == 0:
        return f"{int(normalized_number) // 10000}万" in normalized_translation
    return False


def evaluate_case(case: dict[str, Any], translated: str) -> list[str]:
    """Evaluate one candidate using the shared quality gate plus fixture term equivalences."""
    issues = translation_quality_issues(case["source"], translated)
    protected_terms = case["protected_terms"]
    filtered_issues: list[str] = []
    for issue in issues:
        if issue == "condition:more than" and re.search(r"\bno more than\b", case["source"].lower()):
            continue
        if issue.startswith("number:") and _number_present(issue.removeprefix("number:"), translated):
            continue
        if issue.startswith("name:"):
            term = issue.removeprefix("name:")
            if any(value in translated for value in protected_terms.get(term, [])):
                continue
        filtered_issues.append(issue)
    for number in case["required_numbers"]:
        if not _number_present(number, translated):
            filtered_issues.append(f"number:{number}")
    for term in case["required_terms"]:
        if term not in translated:
            filtered_issues.append(f"term:{term}")
    for term, accepted in protected_terms.items():
        if not any(value in translated for value in accepted):
            filtered_issues.append(f"protected_term:{term}")
    return list(dict.fromkeys(filtered_issues))


def evaluate_cases(cases: list[dict[str, Any]], translations: list[str]) -> dict[str, Any]:
    if len(cases) != len(translations):
        raise FixtureValidationError("translation count did not match fixture cases")
    results: list[dict[str, Any]] = []
    failure_counts: dict[str, int] = {}
    category_counts: dict[str, dict[str, int]] = {}
    for case, translated in zip(cases, translations):
        failure_codes = evaluate_case(case, translated)
        actual_status = "failed" if failure_codes else "passed"
        status = "passed" if actual_status == case["expected_quality_status"] else "failed"
        category = category_counts.setdefault(case["category"], {"total": 0, "passed": 0, "failed": 0})
        category["total"] += 1
        category[status] += 1
        if status == "failed":
            for code in failure_codes or ["expected_quality_status_mismatch"]:
                failure_counts[code] = failure_counts.get(code, 0) + 1
        results.append({"id": case["id"], "category": case["category"], "status": status, "failure_codes": failure_codes})
    passed = sum(result["status"] == "passed" for result in results)
    total = len(results)
    return {
        "total": total,
        "passed": passed,
        "failed": total - passed,
        "pass_rate": passed / total if total else 1.0,
        "failure_counts": failure_counts,
        "category_counts": category_counts,
        "cases": results,
    }


def select_cases(cases: list[dict[str, Any]], case_id: str | None, category: str | None, limit: int | None) -> list[dict[str, Any]]:
    selected = [
        case for case in cases
        if (case_id is None or case["id"] == case_id) and (category is None or case["category"] == category)
    ]
    return selected[:limit] if limit is not None else selected


def azure_translations(translator: Any, cases: list[dict[str, Any]], target_language: str) -> list[str]:
    payloads: list[TranslationPayload] = translator.translate_batch(
        [(case["source"], "", "", target_language) for case in cases]
    )
    if len(payloads) != len(cases) or any(not payload.title for payload in payloads):
        raise FixtureValidationError("Azure provider returned incomplete translations")
    return [payload.title for payload in payloads]
