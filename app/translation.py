from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import hashlib
import re
import sqlite3
import time
from typing import Any, Protocol
import unicodedata
from uuid import uuid4

import httpx

from app.config import Settings


TRANSLATION_SUCCESS = "success"
TRANSLATION_FAILED = "failed"


def _normalize_logic_text(value: str) -> str:
    normalized = unicodedata.normalize("NFKC", value).lower()
    normalized = normalized.replace("、", " ").replace("。", " ")
    return re.sub(r"\s+", " ", normalized).strip()


def _matches_any(value: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, value) for pattern in patterns)


def translation_logic_issues(source: str, translated: str) -> list[str]:
    """Detect whether prediction-market boundary and logical operators survived translation."""
    english = _normalize_logic_text(source)
    japanese = _normalize_logic_text(translated)
    issues: list[str] = []

    has_on_or_before = bool(re.search(r"\bon or before\b", english))
    has_on_or_after = bool(re.search(r"\bon or after\b", english))
    has_no_more_than = bool(re.search(r"\bno more than\b", english))

    if has_on_or_before:
        if not _matches_any(japanese, (r"以前", r"までに", r"当日.*(?:以前|まで)", r"以下の日付まで")) or "より前" in japanese:
            issues.append("logic_on_or_before_not_preserved")
    else:
        if re.search(r"\bbefore\b", english) and not _matches_any(japanese, (r"より前", r"以前", r"前まで")):
            issues.append("logic_before_not_preserved")
        if re.search(r"\bby\b", english):
            if not _matches_any(japanese, (r"までに", r"期限まで", r"期日まで")) or "より前" in japanese:
                issues.append("logic_by_not_preserved")

    if has_on_or_after:
        if not _matches_any(japanese, (r"以降", r"当日.*以降", r"当日またはそれ以降")) or "より後" in japanese:
            issues.append("logic_on_or_after_not_preserved")
    elif re.search(r"\bafter\b", english) and (not _matches_any(japanese, (r"より後", r"後に")) or "以降" in japanese):
        issues.append("logic_after_not_preserved")

    if re.search(r"\bat least\b", english):
        if not _matches_any(japanese, (r"少なくとも", r"以上")) or _matches_any(japanese, (r"未満", r"以下")):
            issues.append("logic_at_least_not_preserved")
    if has_no_more_than:
        if not _matches_any(japanese, (r"以下", r"多くても", r"超えない")) or "未満" in japanese:
            issues.append("logic_no_more_than_not_preserved")
    elif re.search(r"\bmore than\b", english):
        if not _matches_any(japanese, (r"超える", r"より多い")) or "以上" in japanese:
            issues.append("logic_more_than_not_preserved")
    if re.search(r"\bless than\b", english):
        if not _matches_any(japanese, (r"未満", r"より少ない")) or "以下" in japanese:
            issues.append("logic_less_than_not_preserved")

    if re.search(r"\bonly if\b", english):
        if not _matches_any(japanese, (r"場合に限り", r"場合にのみ", r"のときだけ", r"場合だけ")):
            issues.append("logic_only_if_not_preserved")

    negation_source = bool(re.search(r"\bnot\b", english) or re.search(r"\bwithout\b", english))
    negation_source = negation_source or bool(re.search(r"\bno\b", english) and not has_no_more_than)
    if negation_source and not _matches_any(japanese, (r"ない", r"ではない", r"せず", r"なし", r"除く", r"得ず", r"なければ")):
        issues.append("logic_negation_not_preserved")

    if re.search(r"\bbetween\b.+\band\b", english):
        if not _matches_any(japanese, (r"から.+(?:まで|の間)", r"と.+の間", r"以上.+以下")):
            issues.append("logic_between_not_preserved")
    if re.search(r"\bexactly\b", english):
        if not _matches_any(japanese, (r"ちょうど", r"正確に", r"のみ")) or _matches_any(japanese, (r"少なくとも", r"以上", r"以下")):
            issues.append("logic_exactly_not_preserved")
    if re.search(r"\beither\b.+\bor\b", english):
        if not _matches_any(japanese, (r"または", r"又は", r"いずれか", r"か")) or _matches_any(japanese, (r"両方", r"どちらも", r"および")):
            issues.append("logic_either_or_not_preserved")
    if re.search(r"\bboth\b.+\band\b", english):
        if not _matches_any(japanese, (r"両方", r"両国", r"および", r"かつ")) or _matches_any(japanese, (r"または", r"又は", r"いずれか")):
            issues.append("logic_both_and_not_preserved")
    return issues


def translation_quality_issues(source: str, translated: str) -> list[str]:
    """Return conservative, machine-detectable fidelity failures for saved translations."""
    source = normalize_source_text(source)
    translated = normalize_source_text(translated)
    issues: list[str] = []
    if not translated:
        return ["empty"]
    if translated == source:
        issues.append("unchanged")

    source_numbers = re.findall(r"(?<![\w.])\d+(?:,\d{3})*(?:\.\d+)?", source)
    normalized_translation = translated.replace(",", "").replace("，", "")
    for number in source_numbers:
        if number.replace(",", "") not in normalized_translation:
            issues.append(f"number:{number}")

    for url in re.findall(r"https?://\S+", source):
        if url not in translated:
            issues.append("url")
    protected_names = re.findall(
        r"\b(?:NATO|Bitcoin|Federal Reserve|United Nations|Japan|Alice|Bob|Kraken)\b",
        source,
    )
    for name in protected_names:
        if name not in translated:
            issues.append(f"name:{name}")

    required_conditions = {
        "at least": r"少なくとも|以上",
        "no more than": r"以下|を超えない",
        "more than": r"より多|超える|以上",
        "less than": r"未満|より少な",
        "on or before": r"までに|以前|まで",
        "before": r"前|以前|までに",
        "after": r"後|以降",
        "not ": r"ない|ません|ず",
    }
    lowered = source.lower()
    for condition, expected in required_conditions.items():
        if condition in lowered and not re.search(expected, translated):
            issues.append(f"condition:{condition}")
    issues.extend(translation_logic_issues(source, translated))
    return issues


class TranslationUnavailableError(RuntimeError):
    """Raised when no configured translator can produce a translation."""


@dataclass(frozen=True)
class TranslationPayload:
    title: str
    question: str
    description: str | None = None
    provider: str = "unknown"
    model: str = "unknown"


class LogicProtectionError(TranslationUnavailableError):
    """Raised when Azure logic markers cannot be restored safely."""


@dataclass(frozen=True)
class ProtectedLogicSpan:
    marker_id: str
    operator: str
    source_text: str
    target_text: str
    start: int
    end: int


_LOGIC_DATE_TARGET = r"(?:[A-Z][a-z]+\s+\d{1,2},?\s+\d{4}|[A-Z][a-z]+\s+\d{4}|[A-Z][a-z]+\s+\d{1,2}(?!\d)|Q[1-4]\s+\d{4}|\d{4}|the\s+resolution\s+date)"


_LOGIC_PATTERNS = (
    ("on_or_before", re.compile(rf"\bon or before\s+(?P<target>{_LOGIC_DATE_TARGET})", re.I)),
    ("on_or_after", re.compile(rf"\bon or after\s+(?P<target>{_LOGIC_DATE_TARGET})", re.I)),
    ("no_more_than", re.compile(r"\bno more than\s+(?P<target>(?:\$?[\d,]+|one|two|three|five|ten)(?:\s+(?:percent|%|countries|country|votes))?)", re.I)),
    ("at_least", re.compile(r"\bat least\s+(?P<target>(?:\$?[\d,]+|one|two|three|five|ten)(?:\s+(?:percent|%|countries|country|votes))?)", re.I)),
    ("more_than", re.compile(r"\bmore than\s+(?P<target>(?:\$?[\d,]+|one|two|three|five|ten)(?:\s+(?:percent|%|countries|country|votes))?)", re.I)),
    ("less_than", re.compile(r"\bless than\s+(?P<target>(?:\$?[\d,]+|one|two|three|five|ten)(?:\s+(?:percent|%|countries|country|votes))?)", re.I)),
    ("exactly", re.compile(r"\bexactly\s+(?P<target>(?:\$?[\d,]+|one|two|three|five|ten)(?:\s+(?:percent|%|countries|country|votes))?)", re.I)),
    ("before", re.compile(rf"\bbefore\s+(?P<target>{_LOGIC_DATE_TARGET})", re.I)),
    ("after", re.compile(rf"\bafter\s+(?P<target>{_LOGIC_DATE_TARGET})", re.I)),
    ("by", re.compile(rf"\bby\s*(?P<target>{_LOGIC_DATE_TARGET}|\.\.\.)", re.I)),
    ("only_if", re.compile(r"\bonly if\s+(?P<target>[^,.;?!]+)", re.I)),
)


def protect_translation_logic(text: str, marker_seed: str) -> tuple[str, list[ProtectedLogicSpan]]:
    candidates: list[tuple[int, int, str, str]] = []
    for operator, pattern in _LOGIC_PATTERNS:
        for match in pattern.finditer(text):
            if operator == "only_if" and re.search(r"\bon or (?:before|after)\b", match.group("target"), re.I):
                continue
            candidates.append((match.start(), match.end(), operator, match.group("target").strip()))
    candidates.sort(key=lambda item: (item[0], -(item[1] - item[0])))
    selected: list[tuple[int, int, str, str]] = []
    for candidate in candidates:
        if not any(candidate[0] < end and start < candidate[1] for start, end, _, _ in selected):
            selected.append(candidate)
    spans: list[ProtectedLogicSpan] = []
    output = text
    for index, (start, end, operator, target) in reversed(list(enumerate(selected))):
        marker_id = f"{marker_seed}{index:04d}"
        token = f"DPMLOGIC{marker_id}X"
        output = output[:start] + token + output[end:]
        spans.append(ProtectedLogicSpan(marker_id, operator, text[start:end], target, start, end))
    return output, list(reversed(spans))


def restore_translation_logic(translated: str, spans: list[ProtectedLogicSpan]) -> str:
    restored = translated
    templates = {
        "before": "{target}より前", "by": "{target}までに", "on_or_before": "{target}以前",
        "after": "{target}より後", "on_or_after": "{target}以降", "at_least": "少なくとも{target}",
        "more_than": "{target}を超える", "less_than": "{target}未満", "no_more_than": "{target}以下",
        "exactly": "ちょうど{target}", "only_if": "{target}の場合に限り",
    }
    for span in spans:
        token = f"DPMLOGIC{span.marker_id}X"
        if restored.count(token) != 1:
            raise LogicProtectionError("Azure Translator logic marker count mismatch")
        target = _logic_target_japanese(span.target_text)
        replacement = templates[span.operator].format(target=target)
        if span.operator == "before" and span.target_text.lower() == "the resolution date":
            replacement = f"{target}以前"
        restored = restored.replace(token, replacement)
    if "DPMLOGIC" in restored:
        raise LogicProtectionError("Azure Translator returned an unresolved logic marker")
    return restored


def _logic_target_japanese(target: str) -> str:
    months = {"January": "1月", "February": "2月", "March": "3月", "April": "4月", "May": "5月", "June": "6月", "July": "7月", "August": "8月", "September": "9月", "October": "10月", "November": "11月", "December": "12月"}
    match = re.fullmatch(r"([A-Z][a-z]+)\s+(\d{1,2}),?\s+(\d{4})", target)
    if match and match.group(1) in months:
        return f"{match.group(3)}年{months[match.group(1)]}{match.group(2)}日"
    match = re.fullmatch(r"([A-Z][a-z]+)\s+(\d{4})", target)
    if match and match.group(1) in months:
        return f"{match.group(2)}年{months[match.group(1)]}"
    words = {"one": "1", "two": "2", "three": "3", "five": "5", "ten": "10"}
    converted = target
    for english, japanese in words.items():
        converted = re.sub(rf"\b{english}\b", japanese, converted, flags=re.I)
    converted = re.sub(r"\s*percent\b", "%", converted, flags=re.I)
    converted = re.sub(r"\s*countries?\b", "か国", converted, flags=re.I)
    converted = re.sub(r"\s*votes\b", "票", converted, flags=re.I)
    converted = re.sub(r"^\$([\d,]+)$", r"\1ドル", converted)
    return {
        "audited": "監査を受けた",
        "the event occurs": "イベントが発生する",
        "the resolution date": "解決日",
    }.get(converted.lower(), converted)


class Translator(Protocol):
    def translate(
        self,
        *,
        title: str,
        question: str,
        description: str,
        target_language: str,
    ) -> TranslationPayload: ...


class NoopTranslator:
    provider = "noop"
    model = "none"

    def translate(
        self,
        *,
        title: str,
        question: str,
        description: str,
        target_language: str,
    ) -> TranslationPayload:
        raise TranslationUnavailableError("no translation provider is configured")


class AzureTranslatorError(TranslationUnavailableError):
    """Raised when Azure Translator cannot return a valid translation response."""


class AzureTranslator:
    """Azure Translator REST provider with bounded retries for transient failures."""

    provider = "azure"
    _retry_statuses = {408, 429, 500, 502, 503, 504}
    _outcome_pattern = re.compile(r"(?<![A-Za-z0-9_])(Yes|No)(?![A-Za-z0-9_])")
    _outcome_placeholder_pattern = re.compile(r"__AZURE_OUTCOME_(?:YES|NO)_\d+_A19F__")

    def __init__(
        self,
        *,
        key: str,
        endpoint: str = "https://api.cognitive.microsofttranslator.com",
        region: str = "",
        api_version: str = "3.0",
        source_language: str = "en",
        target_language: str = "ja",
        timeout_seconds: int = 15,
        max_retries: int = 3,
        batch_size: int = 20,
        http_client: Any | None = None,
        sleep: Any = time.sleep,
        logic_marker_seed_factory: Any = lambda: uuid4().hex[:12].upper(),
    ) -> None:
        self.key = key
        self.endpoint = endpoint.rstrip("/")
        self.region = region
        self.api_version = api_version
        self.source_language = source_language
        self.target_language = target_language
        self.timeout_seconds = max(1, int(timeout_seconds))
        self.max_retries = max(0, int(max_retries))
        self.batch_size = max(1, int(batch_size))
        self.model = f"azure-translator-{api_version}"
        self._http_client = http_client
        self._sleep = sleep
        self._logic_marker_seed_factory = logic_marker_seed_factory

    def _headers(self) -> dict[str, str]:
        headers = {
            "Ocp-Apim-Subscription-Key": self.key,
            "Content-Type": "application/json; charset=UTF-8",
            "X-ClientTraceId": str(uuid4()),
        }
        if self.region:
            headers["Ocp-Apim-Subscription-Region"] = self.region
        return headers

    @staticmethod
    def _retry_after_seconds(headers: Any) -> float | None:
        value = headers.get("Retry-After") if headers else None
        if not value:
            return None
        try:
            return min(30.0, max(0.0, float(value)))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(value)
            except (TypeError, ValueError, IndexError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=timezone.utc)
            return min(30.0, max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds()))

    def _wait_before_retry(self, attempt: int, headers: Any = None) -> None:
        delay = self._retry_after_seconds(headers)
        if delay is None:
            delay = min(5.0, 0.25 * (2 ** attempt))
        self._sleep(delay)

    def _post(self, texts: list[str], target_language: str) -> list[str]:
        if not self.key:
            raise AzureTranslatorError("Azure Translator key is not configured")
        client = self._http_client or httpx.Client()
        close_client = self._http_client is None
        try:
            for attempt in range(self.max_retries + 1):
                try:
                    response = client.post(
                        f"{self.endpoint}/translate",
                        params={
                            "api-version": self.api_version,
                            "from": self.source_language,
                            "to": target_language,
                            "textType": "plain",
                        },
                        headers=self._headers(),
                        json=[{"Text": text} for text in texts],
                        timeout=self.timeout_seconds,
                    )
                except httpx.TimeoutException:
                    if attempt < self.max_retries:
                        self._wait_before_retry(attempt)
                        continue
                    raise AzureTranslatorError("Azure Translator request timed out after retries") from None
                except httpx.RequestError:
                    if attempt < self.max_retries:
                        self._wait_before_retry(attempt)
                        continue
                    raise AzureTranslatorError("Azure Translator request failed after retries") from None

                if response.status_code in self._retry_statuses:
                    if attempt < self.max_retries:
                        self._wait_before_retry(attempt, response.headers)
                        continue
                    raise AzureTranslatorError(f"Azure Translator returned HTTP {response.status_code} after retries")
                if response.status_code >= 400:
                    raise AzureTranslatorError(f"Azure Translator returned HTTP {response.status_code}")
                return self._parse_response(response, len(texts))
        finally:
            if close_client:
                client.close()
        raise AzureTranslatorError("Azure Translator request failed")

    @staticmethod
    def _parse_response(response: Any, expected_count: int) -> list[str]:
        try:
            payload = response.json()
        except Exception:
            raise AzureTranslatorError("Azure Translator returned an invalid response") from None
        if not isinstance(payload, list) or len(payload) != expected_count:
            raise AzureTranslatorError("Azure Translator response count did not match request")
        results: list[str] = []
        for item in payload:
            if not isinstance(item, dict) or not isinstance(item.get("translations"), list) or not item["translations"]:
                raise AzureTranslatorError("Azure Translator response is missing translations")
            translation = item["translations"][0]
            if not isinstance(translation, dict) or not isinstance(translation.get("text"), str) or not translation["text"]:
                raise AzureTranslatorError("Azure Translator response is missing translated text")
            if not isinstance(translation.get("to"), str) or not translation["to"]:
                raise AzureTranslatorError("Azure Translator response is missing target language")
            results.append(translation["text"])
        return results

    def _translate_texts(self, values: list[str], target_language: str) -> list[str]:
        results = ["" for _ in values]
        positions = [index for index, value in enumerate(values) if value]
        for start in range(0, len(positions), self.batch_size):
            indexes = positions[start:start + self.batch_size]
            protected_texts: list[str] = []
            replacements: list[dict[str, str]] = []
            logic_spans: list[list[ProtectedLogicSpan]] = []
            for index in indexes:
                protected_logic, spans = protect_translation_logic(values[index], self._logic_marker_seed_factory())
                protected, mapping = self._protect_outcomes(protected_logic)
                protected_texts.append(protected)
                replacements.append(mapping)
                logic_spans.append(spans)
            translated_batch = self._post(protected_texts, target_language)
            for index, translated, mapping, spans in zip(indexes, translated_batch, replacements, logic_spans):
                results[index] = restore_translation_logic(self._restore_outcomes(translated, mapping), spans)
        return results

    @classmethod
    def _protect_outcomes(cls, text: str) -> tuple[str, dict[str, str]]:
        replacements: dict[str, str] = {}

        def replace(match: re.Match[str]) -> str:
            outcome = match.group(1)
            token = f"__AZURE_OUTCOME_{outcome.upper()}_{len(replacements)}_A19F__"
            while token in text or token in replacements:
                token = f"__AZURE_OUTCOME_{outcome.upper()}_{len(replacements) + 1}_A19F__"
            replacements[token] = outcome
            return token

        return cls._outcome_pattern.sub(replace, text), replacements

    @classmethod
    def _restore_outcomes(cls, translated: str, replacements: dict[str, str]) -> str:
        restored = translated
        for token, outcome in replacements.items():
            restored = restored.replace(token, outcome)
        if cls._outcome_placeholder_pattern.search(restored):
            raise AzureTranslatorError("Azure Translator returned an unresolved outcome placeholder")
        return restored

    def translate_batch(self, requests: list[tuple[str, str, str, str]]) -> list[TranslationPayload]:
        target_language = requests[0][3] if requests else self.target_language
        titles = self._translate_texts([title for title, _, _, _ in requests], target_language)
        questions = self._translate_texts([question for _, question, _, _ in requests], target_language)
        descriptions = self._translate_texts([description for _, _, description, _ in requests], target_language)
        return [
            TranslationPayload(title=title, question=question, description=description or None, provider=self.provider, model=self.model)
            for title, question, description in zip(titles, questions, descriptions)
        ]

    def translate(self, *, title: str, question: str, description: str, target_language: str) -> TranslationPayload:
        return self.translate_batch([(title, question, description, target_language or self.target_language)])[0]


class LocalMarianTranslator:
    """Lazy, reusable local Marian translator used only by translation CLI jobs."""

    provider = "local_marian"

    def __init__(
        self,
        *,
        model_id: str,
        device: str = "auto",
        batch_size: int = 4,
        local_files_only: bool = False,
        max_input_length: int = 256,
        torch_module: Any | None = None,
        tokenizer_class: Any | None = None,
        model_class: Any | None = None,
    ) -> None:
        self.model_id = model_id
        self.model = model_id
        self.requested_device = device if device in {"auto", "cpu", "cuda"} else "auto"
        self.batch_size = max(1, min(int(batch_size), 32))
        self.local_files_only = local_files_only
        self.max_input_length = max(16, min(int(max_input_length), 512))
        self._torch = torch_module
        self._tokenizer_class = tokenizer_class
        self._model_class = model_class
        self._tokenizer: Any | None = None
        self._model: Any | None = None
        self.device_used: str | None = None
        self.cpu_fallback_used = False

    def _dependencies(self) -> tuple[Any, Any, Any]:
        if self._torch is not None and self._tokenizer_class is not None and self._model_class is not None:
            return self._torch, self._tokenizer_class, self._model_class
        try:
            import torch
            from transformers import AutoModelForSeq2SeqLM, AutoTokenizer
        except ImportError as exc:
            raise TranslationUnavailableError(
                "local_marian requires translation dependencies; install with "
                "python -m pip install -r requirements-translation.txt"
            ) from exc
        self._torch = torch
        self._tokenizer_class = AutoTokenizer
        self._model_class = AutoModelForSeq2SeqLM
        return torch, AutoTokenizer, AutoModelForSeq2SeqLM

    def _preferred_device(self, torch: Any) -> str:
        if self.requested_device == "cpu":
            return "cpu"
        if self.requested_device == "cuda":
            return "cuda"
        return "cuda" if bool(torch.cuda.is_available()) else "cpu"

    def _load(self, device: str) -> None:
        _, tokenizer_class, model_class = self._dependencies()
        tokenizer = tokenizer_class.from_pretrained(self.model_id, local_files_only=self.local_files_only)
        model = model_class.from_pretrained(self.model_id, local_files_only=self.local_files_only)
        model.to(device)
        model.eval()
        self._tokenizer = tokenizer
        self._model = model
        self.device_used = device

    def _ensure_loaded(self) -> None:
        if self._model is not None and self._tokenizer is not None:
            return
        torch, _, _ = self._dependencies()
        device = self._preferred_device(torch)
        try:
            self._load(device)
        except Exception as exc:
            self._model = self._tokenizer = None
            if device != "cuda":
                if isinstance(exc, TranslationUnavailableError):
                    raise
                raise TranslationUnavailableError("local Marian model could not be loaded") from exc
            self.cpu_fallback_used = True
            try:
                self._load("cpu")
            except Exception as cpu_exc:
                self._model = self._tokenizer = None
                raise TranslationUnavailableError("local Marian model could not be loaded on CUDA or CPU") from cpu_exc

    def _translate_texts(self, values: list[str]) -> list[str]:
        results = ["" for _ in values]
        positions = [index for index, value in enumerate(values) if value]
        if not positions:
            return results
        self._ensure_loaded()
        for start in range(0, len(positions), self.batch_size):
            indexes = positions[start:start + self.batch_size]
            texts = [values[index] for index in indexes]
            translated = self._generate(texts)
            if len(translated) != len(texts) or any(not value.strip() for value in translated):
                raise TranslationUnavailableError("local Marian produced an empty translation")
            for index, value in zip(indexes, translated):
                results[index] = value.strip()
        return results

    def _generate(self, texts: list[str]) -> list[str]:
        torch, _, _ = self._dependencies()
        assert self._tokenizer is not None and self._model is not None and self.device_used is not None
        try:
            encoded = self._tokenizer(
                texts,
                return_tensors="pt",
                padding=True,
                truncation=True,
                max_length=self.max_input_length,
            )
            encoded = {key: value.to(self.device_used) for key, value in encoded.items()}
            with torch.inference_mode():
                generated = self._model.generate(**encoded)
            return list(self._tokenizer.batch_decode(generated, skip_special_tokens=True))
        except Exception as exc:
            if self.device_used != "cuda" or self.cpu_fallback_used:
                raise TranslationUnavailableError("local Marian inference failed") from exc
            self.cpu_fallback_used = True
            self._model = self._tokenizer = None
            try:
                self._load("cpu")
            except Exception as cpu_exc:
                raise TranslationUnavailableError("local Marian inference failed on CUDA and CPU") from cpu_exc
            return self._generate(texts)

    def translate_batch(
        self,
        requests: list[tuple[str, str, str, str]],
    ) -> list[TranslationPayload]:
        titles = self._translate_texts([title for title, _, _, _ in requests])
        questions = self._translate_texts([question for _, question, _, _ in requests])
        descriptions = self._translate_texts([description for _, _, description, _ in requests])
        return [
            TranslationPayload(
                title=title,
                question=question,
                description=description or None,
                provider=self.provider,
                model=self.model_id,
            )
            for title, question, description in zip(titles, questions, descriptions)
        ]

    def translate(
        self,
        *,
        title: str,
        question: str,
        description: str,
        target_language: str,
    ) -> TranslationPayload:
        return self.translate_batch([(title, question, description, target_language)])[0]

    def validate_payload(
        self,
        *,
        title: str,
        question: str,
        description: str,
        payload: TranslationPayload,
    ) -> list[str]:
        issues = translation_quality_issues(title, payload.title)
        issues.extend(f"question.{issue}" for issue in translation_quality_issues(question, payload.question))
        if description:
            issues.extend(f"description.{issue}" for issue in translation_quality_issues(description, payload.description or ""))
        return issues

    def summary(self) -> dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model_id,
            "device": self.device_used or "not_loaded",
            "cpu_fallback": self.cpu_fallback_used,
        }


def get_translator(settings: Settings) -> Translator:
    if settings.translation_provider == "azure":
        return AzureTranslator(
            key=settings.azure_translator_key,
            endpoint=settings.azure_translator_endpoint,
            region=settings.azure_translator_region,
            api_version=settings.azure_translator_api_version,
            source_language=settings.azure_translator_source_language,
            target_language=settings.azure_translator_target_language,
            timeout_seconds=settings.azure_translator_timeout_seconds,
            max_retries=settings.azure_translator_max_retries,
            batch_size=settings.azure_translator_batch_size,
        )
    if settings.translation_provider == "local_marian":
        return LocalMarianTranslator(
            model_id=settings.translation_model,
            device=settings.translation_device,
            batch_size=settings.translation_batch_size,
            local_files_only=settings.translation_local_files_only,
        )
    return NoopTranslator()


def normalize_source_text(value: str | None) -> str:
    text = str(value or "").replace("\r\n", "\n").replace("\r", "\n")
    lines = [line.strip() for line in text.strip().split("\n")]
    return re.sub(r"\n{3,}", "\n\n", "\n".join(lines))


def source_hash(value: str | None) -> str:
    return hashlib.sha256(normalize_source_text(value).encode("utf-8")).hexdigest()


def market_source_hashes(market: dict[str, Any]) -> dict[str, str]:
    return {
        "source_title_hash": source_hash(market.get("title")),
        "source_question_hash": source_hash(market.get("question")),
        "source_description_hash": source_hash(market.get("description")),
    }


def get_market_translation(conn: sqlite3.Connection, market_id: str, language: str) -> dict[str, Any] | None:
    row = conn.execute(
        "select * from market_translations where market_id = ? and language = ?",
        (market_id, language),
    ).fetchone()
    return dict(row) if row else None


def get_market_translations(
    conn: sqlite3.Connection,
    market_ids: list[str],
    language: str,
) -> dict[str, dict[str, Any]]:
    if not market_ids:
        return {}
    placeholders = ", ".join("?" for _ in market_ids)
    rows = conn.execute(
        f"select * from market_translations where language = ? and market_id in ({placeholders})",
        [language, *market_ids],
    ).fetchall()
    return {str(row["market_id"]): dict(row) for row in rows}


def upsert_market_translation(
    conn: sqlite3.Connection,
    *,
    market: dict[str, Any],
    language: str,
    translated_title: str | None,
    translated_question: str | None,
    translated_description: str | None,
    provider: str,
    model: str,
    status: str,
    error_message: str | None = None,
) -> None:
    hashes = market_source_hashes(market)
    conn.execute(
        """
        insert into market_translations(
            market_id, language, translated_title, translated_question, translated_description,
            source_title_hash, source_question_hash, source_description_hash,
            translation_provider, translation_model, translation_status, translated_at, error_message
        ) values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
        on conflict(market_id, language) do update set
            translated_title = excluded.translated_title,
            translated_question = excluded.translated_question,
            translated_description = excluded.translated_description,
            source_title_hash = excluded.source_title_hash,
            source_question_hash = excluded.source_question_hash,
            source_description_hash = excluded.source_description_hash,
            translation_provider = excluded.translation_provider,
            translation_model = excluded.translation_model,
            translation_status = excluded.translation_status,
            translated_at = excluded.translated_at,
            error_message = excluded.error_message
        """,
        (
            market["market_id"],
            language,
            translated_title,
            translated_question,
            translated_description,
            hashes["source_title_hash"],
            hashes["source_question_hash"],
            hashes["source_description_hash"],
            provider,
            model,
            status,
            datetime.now(timezone.utc).isoformat(),
            error_message,
        ),
    )
    conn.commit()


def translation_source_matches(row: dict[str, Any] | None, market: dict[str, Any]) -> bool:
    if not row:
        return False
    return all(row.get(key) == value for key, value in market_source_hashes(market).items())


def translation_matches_source(row: dict[str, Any] | None, market: dict[str, Any]) -> bool:
    return bool(row and row.get("translation_status") == TRANSLATION_SUCCESS and translation_source_matches(row, market))


def add_translation_display(
    conn: sqlite3.Connection,
    market: dict[str, Any],
    *,
    language: str,
    enabled: bool,
    translation: dict[str, Any] | None = None,
) -> dict[str, Any]:
    item = dict(market)
    original_title = str(item.get("title") or "")
    original_question = str(item.get("question") or "")
    original_description = str(item.get("description") or "")
    row = translation if translation is not None else (
        get_market_translation(conn, str(item.get("market_id") or ""), "ja") if language == "ja" and enabled else None
    )
    available = bool(enabled and language == "ja" and translation_matches_source(row, item))
    item.update(
        {
            "original_title": original_title,
            "original_question": original_question,
            "original_description": original_description,
            "display_title": str(row.get("translated_title") or original_title) if available and row else original_title,
            "display_question": str(row.get("translated_question") or original_question) if available and row else original_question,
            "display_description": str(row.get("translated_description") or original_description) if available and row else original_description,
            "translation_available": available,
            "display_language": "ja" if available else "original",
        }
    )
    return item


def add_translation_displays(
    conn: sqlite3.Connection,
    markets: list[dict[str, Any]],
    *,
    language: str,
    enabled: bool,
) -> list[dict[str, Any]]:
    translations = get_market_translations(
        conn,
        [str(market.get("market_id") or "") for market in markets],
        "ja",
    ) if language == "ja" and enabled else {}
    return [
        add_translation_display(
            conn,
            market,
            language=language,
            enabled=enabled,
            translation=translations.get(str(market.get("market_id") or "")),
        )
        for market in markets
    ]
