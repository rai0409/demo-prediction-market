import importlib.util
import sys
import types

from app.config import Settings
from app.translation import LocalMarianTranslator, TranslationPayload, TranslationUnavailableError, get_translator, translation_quality_issues


class FakeTensor:
    def __init__(self, count):
        self.count = count

    def to(self, device):
        return self


class FakeTorch:
    class cuda:
        available = False

        @classmethod
        def is_available(cls):
            return cls.available

    class inference_mode:
        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False


class FakeTokenizer:
    calls = []
    from_pretrained_calls = []

    @classmethod
    def from_pretrained(cls, model_id, **kwargs):
        cls.from_pretrained_calls.append((model_id, kwargs))
        return cls()

    def __call__(self, texts, **kwargs):
        type(self).calls.append((list(texts), kwargs))
        return {"input_ids": FakeTensor(len(texts))}

    def batch_decode(self, generated, skip_special_tokens=True):
        return [f"訳{index}" for index in range(generated)]


class FakeModel:
    from_pretrained_calls = []
    fail_cuda_load = False
    fail_cuda_generate = False

    @classmethod
    def from_pretrained(cls, model_id, **kwargs):
        cls.from_pretrained_calls.append((model_id, kwargs))
        return cls()

    def to(self, device):
        self.device = device
        if device == "cuda" and type(self).fail_cuda_load:
            raise RuntimeError("cuda load failed")
        return self

    def eval(self):
        self.evaluated = True
        return self

    def generate(self, **encoded):
        if self.device == "cuda" and type(self).fail_cuda_generate:
            raise RuntimeError("cuda inference failed")
        return encoded["input_ids"].count


def _translator(**kwargs):
    FakeTokenizer.calls = []
    FakeTokenizer.from_pretrained_calls = []
    FakeModel.from_pretrained_calls = []
    FakeModel.fail_cuda_load = False
    FakeModel.fail_cuda_generate = False
    return LocalMarianTranslator(
        model_id="Helsinki-NLP/opus-mt-en-jap",
        torch_module=FakeTorch,
        tokenizer_class=FakeTokenizer,
        model_class=FakeModel,
        **kwargs,
    )


def test_local_marian_factory_is_lazy_and_does_not_load_at_web_factory_time():
    settings = Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", translation_provider="local_marian")
    translator = get_translator(settings)
    assert isinstance(translator, LocalMarianTranslator)
    assert translator.device_used is None


def test_local_marian_reuses_loaded_model_batches_and_skips_empty_values():
    translator = _translator(batch_size=4, local_files_only=True)
    results = translator.translate_batch([("one", "question one", "", "ja"), ("two", "question two", "", "ja")])
    assert [result.title for result in results] == ["訳0", "訳1"]
    assert len(FakeTokenizer.from_pretrained_calls) == 1
    assert len(FakeModel.from_pretrained_calls) == 1
    assert FakeTokenizer.from_pretrained_calls[0][1]["local_files_only"] is True
    assert [call[0] for call in FakeTokenizer.calls] == [["one", "two"], ["question one", "question two"]]
    translator.translate(title="three", question="question three", description="", target_language="ja")
    assert len(FakeTokenizer.from_pretrained_calls) == 1
    empty = _translator()
    assert empty._translate_texts([""]) == [""]
    assert not FakeTokenizer.from_pretrained_calls


def test_local_marian_falls_back_to_cpu_after_cuda_load_or_inference_failure():
    FakeTorch.cuda.available = True
    translator = _translator(device="auto")
    FakeModel.fail_cuda_load = True
    translator.translate(title="one", question="two", description="", target_language="ja")
    assert translator.device_used == "cpu"
    assert translator.cpu_fallback_used is True
    FakeModel.fail_cuda_load = False
    translator = _translator(device="auto")
    FakeModel.fail_cuda_generate = True
    translator.translate(title="one", question="two", description="", target_language="ja")
    assert translator.device_used == "cpu"
    assert translator.cpu_fallback_used is True
    FakeTorch.cuda.available = False


def test_local_marian_reports_dependency_install_instruction(monkeypatch):
    translator = LocalMarianTranslator(model_id="Helsinki-NLP/opus-mt-en-jap")
    original_import = __import__

    def missing_torch(name, *args, **kwargs):
        if name == "torch":
            raise ImportError("missing")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr("builtins.__import__", missing_torch)
    try:
        translator.translate(title="one", question="two", description="", target_language="ja")
    except TranslationUnavailableError as exc:
        assert "requirements-translation.txt" in str(exc)
    else:
        raise AssertionError("expected missing dependency error")


def _load_script(name, path):
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_download_script_verifies_local_reload_with_mocked_dependencies(monkeypatch, capsys):
    calls = []

    class Tokenizer:
        init_kwargs = {"revision": "mock-revision"}

        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            calls.append(("tokenizer", model_id, kwargs))
            return cls()

    class Model:
        config = types.SimpleNamespace(_commit_hash="mock-revision", model_type="marian")

        @classmethod
        def from_pretrained(cls, model_id, **kwargs):
            calls.append(("model", model_id, kwargs))
            return cls()

        def eval(self):
            return self

    fake_torch = types.SimpleNamespace(__version__="mock", cuda=types.SimpleNamespace(is_available=lambda: False))
    fake_transformers = types.SimpleNamespace(AutoTokenizer=Tokenizer, AutoModelForSeq2SeqLM=Model)
    monkeypatch.setitem(sys.modules, "torch", fake_torch)
    monkeypatch.setitem(sys.modules, "transformers", fake_transformers)
    script = _load_script("download_translation_model_test", "scripts/download_translation_model.py")
    assert script.run(["--model-id", "mock/model", "--local-files-only", "--device-check"]) == 0
    assert len(calls) == 4
    assert all(call[2]["local_files_only"] is True for call in calls)
    assert "local_files_only_verification=ok" in capsys.readouterr().out


def test_quality_gate_detects_missing_condition_tokens():
    script = _load_script("translation_quality_test", "scripts/evaluate_translation_quality.py")
    case = {"source": "Will NATO reach 50% by 2026?", "required_tokens": ["NATO", "%"], "required_numbers": ["50", "2026"]}
    assert not script.evaluate_case(case, "NATOは2026年までに50%に達しますか？")
    assert "token:NATO" in script.evaluate_case(case, "2026年までに50%に達しますか？")


def test_translation_quality_gate_requires_numbers_names_and_condition_markers():
    source = "Will NATO be above $100,000 on or before July 31, 2026?"
    assert translation_quality_issues(source, "NATOは2026年7月31日までに$100,000を超えますか？") == []
    issues = translation_quality_issues(source, "それは起きますか？")
    assert "number:100,000" in issues
    assert "name:NATO" in issues
    assert "condition:on or before" in issues
