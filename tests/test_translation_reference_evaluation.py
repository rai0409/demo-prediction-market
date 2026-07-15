import importlib.util
import json
from pathlib import Path

import pytest

from app.translation import TranslationPayload
from app.translation_reference_evaluation import FixtureValidationError, evaluate_case, evaluate_cases, load_fixture


FIXTURE_PATH = Path("tests/fixtures/translation_reference_quality_cases.json")


def _script():
    spec = importlib.util.spec_from_file_location("evaluate_translation_reference_quality", "scripts/evaluate_translation_reference_quality.py")
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def _fixture_copy(tmp_path, mutate=None):
    cases = load_fixture(FIXTURE_PATH)
    if mutate:
        mutate(cases)
    path = tmp_path / "fixture.json"
    path.write_text(json.dumps(cases), encoding="utf-8")
    return path


def test_reference_fixture_has_required_schema_and_known_cases():
    cases = load_fixture(FIXTURE_PATH)
    assert len(cases) >= 30
    assert len({case["id"] for case in cases}) == len(cases)
    sources = {case["source"] for case in cases}
    assert "Will any country leave NATO by December 31, 2026?" in sources
    assert "Will the Federal Reserve cut interest rates before September 2026?" in sources
    assert "Will Bitcoin be above $100,000 on December 31, 2026?" in sources
    assert "Will at least three countries officially recognize the agreement?" in sources
    assert any("Yes" in case["protected_terms"] for case in cases)
    assert any("No" in case["protected_terms"] for case in cases)


def test_fixture_uses_shared_logic_gate_and_equivalences():
    cases = {case["id"]: case for case in load_fixture(FIXTURE_PATH)}
    assert "logic_before_not_preserved" in evaluate_case(cases["before-fed-001"], "連邦準備制度理事会は2026年9月までに利下げを行いますか？")
    assert evaluate_case(cases["currency-bitcoin-001"], cases["currency-bitcoin-001"]["candidate_translation"]) == []
    assert evaluate_case(cases["less-than-rate-001"], "失業率は5%未満になりますか？") == []
    assert cases["multiple-inflation-001"]["expected_logic_operators"] == ["less_than", "more_than"]


def test_full_fixture_matches_expected_quality_statuses():
    cases = load_fixture(FIXTURE_PATH)
    summary = evaluate_cases(cases, [case["candidate_translation"] for case in cases])
    assert summary["total"] == 30
    assert summary["passed"] == 30
    assert summary["failed"] == 0


def test_fixture_validation_rejects_duplicate_id_and_empty_source(tmp_path):
    duplicate = _fixture_copy(tmp_path, lambda cases: cases.__setitem__(1, dict(cases[1], id=cases[0]["id"])))
    with pytest.raises(FixtureValidationError, match="duplicate"):
        load_fixture(duplicate)
    empty_source = _fixture_copy(tmp_path, lambda cases: cases[0].__setitem__("source", ""))
    with pytest.raises(FixtureValidationError, match="source"):
        load_fixture(empty_source)


def test_cli_fixture_default_filters_json_and_limit(capsys):
    script = _script()
    assert script.run(["--json"]) == 0
    default_output = json.loads(capsys.readouterr().out)
    assert default_output["provider"] == "fixture"
    assert "source" not in default_output["cases"][0]
    assert script.run(["--json", "--include-text", "--case-id", "before-fed-001"]) == 0
    diagnostic = json.loads(capsys.readouterr().out)["cases"][0]
    assert {"source", "reference_translation", "candidate_translation"}.issubset(diagnostic)
    assert script.run(["--case-id", "before-fed-001", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1
    assert script.run(["--category", "before", "--limit", "1", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["total"] == 1


def test_cli_fail_on_quality_error_and_invalid_fixture(tmp_path, capsys):
    script = _script()
    assert script.run(["--fail-on-quality-error"]) == 0
    bad_fixture = _fixture_copy(tmp_path, lambda cases: cases[1].__setitem__("candidate_translation", "2026年9月までに利下げを行いますか？"))
    assert script.run(["--fixture", str(bad_fixture), "--fail-on-quality-error"]) == 1
    invalid = tmp_path / "invalid.json"
    invalid.write_text("{}", encoding="utf-8")
    assert script.run(["--fixture", str(invalid)]) == 2
    assert "fixture root" in capsys.readouterr().err


def test_cli_rejects_unknown_provider_and_missing_azure_key(monkeypatch, capsys):
    script = _script()
    assert script.run(["--provider", "unknown"]) == 2
    monkeypatch.delenv("AZURE_TRANSLATOR_KEY", raising=False)
    assert script.run(["--provider", "azure"]) == 2
    assert "Azure Translator key is not configured" in capsys.readouterr().err


def test_cli_azure_provider_is_mocked_and_does_not_touch_database(monkeypatch, tmp_path, capsys):
    cases = load_fixture(FIXTURE_PATH)
    translations = {case["source"]: case["candidate_translation"] for case in cases}

    class FakeAzure:
        def translate_batch(self, requests):
            return [TranslationPayload(title=translations[title], question="", provider="azure", model="fake") for title, *_ in requests]

    database = tmp_path / "untouched.sqlite3"
    monkeypatch.setenv("AZURE_TRANSLATOR_KEY", "test-key-not-for-output")
    monkeypatch.setenv("DEMO_PREDICTION_DB", str(database))
    script = _script()
    assert script.run(["--provider", "azure", "--case-id", "ordinary-sentence-001", "--json"], translator=FakeAzure()) == 0
    output = capsys.readouterr().out
    assert "test-key-not-for-output" not in output
    assert not database.exists()
