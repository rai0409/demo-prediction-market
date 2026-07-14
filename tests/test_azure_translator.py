import httpx
import pytest

from app.config import Settings
from app.translation import AzureTranslator, AzureTranslatorError, LogicProtectionError, get_translator, protect_translation_logic, restore_translation_logic, translation_quality_issues


class FakeResponse:
    def __init__(self, status_code=200, payload=None, headers=None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.headers = headers or {}

    def json(self):
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttpClient:
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.calls = []

    def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _success(*texts):
    return FakeResponse(payload=[{"translations": [{"text": text, "to": "ja"}]} for text in texts])


def _translator(outcomes, **kwargs):
    return AzureTranslator(
        key="secret-azure-key",
        http_client=FakeHttpClient(outcomes),
        sleep=lambda _: None,
        **kwargs,
    )


def test_azure_provider_factory_selection():
    settings = Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", translation_provider="azure")
    assert isinstance(get_translator(settings), AzureTranslator)


def test_azure_sends_region_header_when_configured():
    translator = _translator([_success("題名"), _success("質問")], region="Japan East")
    translator.translate(title="title", question="question", description="", target_language="ja")
    assert translator._http_client.calls[0][1]["headers"]["Ocp-Apim-Subscription-Region"] == "Japan East"


def test_azure_omits_region_header_when_blank():
    translator = _translator([_success("題名"), _success("質問")])
    translator.translate(title="title", question="question", description="", target_language="ja")
    assert "Ocp-Apim-Subscription-Region" not in translator._http_client.calls[0][1]["headers"]


def test_azure_translates_multiple_values_in_input_order():
    translator = _translator([_success("最初", "次"), _success("質問1", "質問2")])
    results = translator.translate_batch([("first", "q1", "", "ja"), ("second", "q2", "", "ja")])
    assert [result.title for result in results] == ["最初", "次"]
    assert [result.question for result in results] == ["質問1", "質問2"]
    assert translator._http_client.calls[0][1]["json"] == [{"Text": "first"}, {"Text": "second"}]


def test_azure_normalizes_endpoint_trailing_slash():
    translator = _translator([_success("訳")], endpoint="https://translator.example/")
    assert translator._translate_texts(["source"], "ja") == ["訳"]
    assert translator._http_client.calls[0][0] == "https://translator.example/translate"


def test_azure_does_not_retry_401_or_leak_key():
    translator = _translator([FakeResponse(status_code=401)])
    with pytest.raises(AzureTranslatorError) as exc_info:
        translator._translate_texts(["source"], "ja")
    assert len(translator._http_client.calls) == 1
    assert "secret-azure-key" not in str(exc_info.value)


def test_azure_retries_429_using_retry_after_then_succeeds():
    delays = []
    client = FakeHttpClient([FakeResponse(status_code=429, headers={"Retry-After": "0"}), _success("訳")])
    translator = AzureTranslator(key="secret-azure-key", http_client=client, sleep=delays.append)
    assert translator._translate_texts(["source"], "ja") == ["訳"]
    assert len(client.calls) == 2
    assert delays == [0.0]


def test_azure_retries_503_then_succeeds():
    translator = _translator([FakeResponse(status_code=503), _success("訳")])
    assert translator._translate_texts(["source"], "ja") == ["訳"]
    assert len(translator._http_client.calls) == 2


def test_azure_retries_timeout_then_succeeds():
    translator = _translator([httpx.ReadTimeout("timed out"), _success("訳")])
    assert translator._translate_texts(["source"], "ja") == ["訳"]
    assert len(translator._http_client.calls) == 2


@pytest.mark.parametrize(
    "payload",
    [
        {},
        [{}],
        [{"translations": []}],
        [{"translations": [{"to": "ja"}]}],
        [{"translations": [{"text": "訳"}]}],
    ],
)
def test_azure_rejects_malformed_response_without_key_leak(payload):
    translator = _translator([FakeResponse(payload=payload)])
    with pytest.raises(AzureTranslatorError) as exc_info:
        translator._translate_texts(["source"], "ja")
    assert "secret-azure-key" not in str(exc_info.value)


def test_azure_rejects_response_count_mismatch():
    translator = _translator([_success("only one")])
    with pytest.raises(AzureTranslatorError, match="count"):
        translator._translate_texts(["one", "two"], "ja")


def test_azure_preserves_yes_no_outcome_labels_without_replacing_normal_words():
    translator = _translator([
        _success("__AZURE_OUTCOME_YES_0_A19F__ / __AZURE_OUTCOME_NO_1_A19F__"),
        _success("question"),
    ])
    result = translator.translate(title="Will Yes or No win?", question="question", description="", target_language="ja")
    assert result.title == "Yes / No"
    sent = translator._http_client.calls[0][1]["json"][0]["Text"]
    assert "__AZURE_OUTCOME_YES_0_A19F__" in sent
    assert "__AZURE_OUTCOME_NO_1_A19F__" in sent
    assert AzureTranslator._protect_outcomes("yesterday is not Yes")[0].startswith("yesterday is not __AZURE_OUTCOME_YES")


def test_azure_rejects_unresolved_outcome_placeholder():
    translator = _translator([_success("__AZURE_OUTCOME_NO_99_A19F__")])
    with pytest.raises(AzureTranslatorError, match="unresolved outcome placeholder"):
        translator._translate_texts(["Yes"], "ja")


@pytest.mark.parametrize(
    ("source", "target", "expected"),
    [
        ("before September 2026", "2026年9月", "2026年9月より前"),
        ("by December 31, 2026", "2026年12月31日", "2026年12月31日までに"),
        ("on or before July 31, 2026", "2026年7月31日", "2026年7月31日以前"),
        ("after January 1, 2027", "2027年1月1日", "2027年1月1日より後"),
        ("on or after January 1, 2027", "2027年1月1日", "2027年1月1日以降"),
        ("more than 50 percent", "50%", "50%を超える"),
        ("no more than 10 percent", "10%", "10%以下"),
        ("only if the event occurs", "イベントが発生する", "イベントが発生するの場合に限り"),
    ],
)
def test_logic_markers_restore_deterministically(source, target, expected):
    protected, spans = protect_translation_logic(source, "TEST")
    marker = spans[0].marker_id
    translated = f"DPMLOGIC{marker}X"
    assert restore_translation_logic(translated, spans) == expected
    assert "DPMLOGIC" in protected


def test_logic_markers_reject_missing_duplicate_and_unresolved_markers():
    _, spans = protect_translation_logic("before September 2026", "TEST")
    marker = spans[0].marker_id
    with pytest.raises(LogicProtectionError):
        restore_translation_logic("", spans)
    with pytest.raises(LogicProtectionError):
        restore_translation_logic(f"DPMLOGIC{marker}X DPMLOGIC{marker}X", spans)
    with pytest.raises(LogicProtectionError):
        restore_translation_logic("DPMLOGICOTHER0000X", spans)


@pytest.mark.parametrize(
    ("source", "operators", "required"),
    [
        ("Taylor Swift pregnant by...?", ["by"], "までに"),
        ("Taylor Swift pregnant by...?", ["by"], "までに"),
        ("Taylor Swift pregnant before 2027?", ["before"], "より前"),
        ("Ukraine recognizes Russian sovereignty over its territory by...?", ["by"], "までに"),
        (
            "A deal is reached by December 31, 2026, 11:59 PM ET. "
            "If it is reached before the resolution date, this market resolves to Yes.",
            ["by", "before"],
            "までに",
        ),
    ],
)
def test_real_market_logic_spans_are_protected_restored_and_quality_safe(source, operators, required):
    protected, spans = protect_translation_logic(source, "REAL")
    assert [span.operator for span in spans] == operators
    assert all(span.source_text for span in spans)
    assert "DPMLOGICREAL" in protected
    for span in spans:
        assert span.source_text not in protected
    translator = _translator([_success(protected)], logic_marker_seed_factory=lambda: "REAL")
    restored = translator._translate_texts([source], "ja")[0]
    assert "DPMLOGIC" not in restored
    assert required in restored
    if "before" in operators:
        assert "より前" in restored or "以前" in restored
    assert not translation_quality_issues(source, restored)
