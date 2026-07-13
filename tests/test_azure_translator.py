import httpx
import pytest

from app.config import Settings
from app.translation import AzureTranslator, AzureTranslatorError, get_translator


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
