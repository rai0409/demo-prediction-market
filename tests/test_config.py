from pathlib import Path

from app.config import get_settings


def test_env_example_lists_limited_operation_controls():
    text = Path(".env.example").read_text()

    assert "DEMO_ADMIN_TOKEN=" in text
    assert "DEMO_PREDICTION_MAX_DEMO_STAKE=" in text
    assert "DEMO_COOKIE_SECURE=" in text


def test_live_env_parsing_enabled_values(monkeypatch):
    for value in ["1", "true", "True", "yes", "on"]:
        monkeypatch.setenv("DEMO_PREDICTION_LIVE", value)
        assert get_settings().live is True


def test_live_env_parsing_disabled_values(monkeypatch):
    for value in ["0", "false", "False", "no", "off", ""]:
        monkeypatch.setenv("DEMO_PREDICTION_LIVE", value)
        assert get_settings().live is False


def test_live_env_default_disabled(monkeypatch):
    monkeypatch.delenv("DEMO_PREDICTION_LIVE", raising=False)
    assert get_settings().live is False


def test_auto_refresh_config_defaults(monkeypatch):
    monkeypatch.delenv("DEMO_PREDICTION_AUTO_REFRESH", raising=False)
    monkeypatch.delenv("DEMO_PREDICTION_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("DEMO_PREDICTION_QUICK_REFRESH_SECONDS", raising=False)
    monkeypatch.delenv("DEMO_PREDICTION_DETAIL_REFRESH_SECONDS", raising=False)
    settings = get_settings()
    assert settings.auto_refresh is False
    assert settings.refresh_seconds == 60
    assert settings.quick_refresh_seconds == 5
    assert settings.detail_refresh_seconds == 3


def test_refresh_seconds_clamp(monkeypatch):
    monkeypatch.setenv("DEMO_PREDICTION_REFRESH_SECONDS", "5")
    assert get_settings().refresh_seconds == 30
    monkeypatch.setenv("DEMO_PREDICTION_REFRESH_SECONDS", "999")
    assert get_settings().refresh_seconds == 300


def test_live_refresh_seconds_clamp(monkeypatch):
    monkeypatch.setenv("DEMO_PREDICTION_QUICK_REFRESH_SECONDS", "1")
    monkeypatch.setenv("DEMO_PREDICTION_DETAIL_REFRESH_SECONDS", "1")
    assert get_settings().quick_refresh_seconds == 3
    assert get_settings().detail_refresh_seconds == 2
    monkeypatch.setenv("DEMO_PREDICTION_QUICK_REFRESH_SECONDS", "99")
    monkeypatch.setenv("DEMO_PREDICTION_DETAIL_REFRESH_SECONDS", "99")
    assert get_settings().quick_refresh_seconds == 30
    assert get_settings().detail_refresh_seconds == 15


def test_websocket_config_defaults(monkeypatch):
    monkeypatch.delenv("DEMO_PREDICTION_WS_ENABLED", raising=False)
    monkeypatch.delenv("DEMO_PREDICTION_WS_TOP_N", raising=False)
    monkeypatch.delenv("DEMO_PREDICTION_WS_STALE_SECONDS", raising=False)
    settings = get_settings()
    assert settings.ws_enabled is False
    assert settings.ws_top_n == 10
    assert settings.ws_stale_seconds == 90


def test_websocket_config_clamps(monkeypatch):
    monkeypatch.setenv("DEMO_PREDICTION_WS_ENABLED", "1")
    monkeypatch.setenv("DEMO_PREDICTION_WS_TOP_N", "999")
    monkeypatch.setenv("DEMO_PREDICTION_WS_STALE_SECONDS", "1")
    settings = get_settings()
    assert settings.ws_enabled is True
    assert settings.ws_top_n == 50
    assert settings.ws_stale_seconds == 15
    monkeypatch.setenv("DEMO_PREDICTION_WS_TOP_N", "0")
    monkeypatch.setenv("DEMO_PREDICTION_WS_STALE_SECONDS", "9999")
    settings = get_settings()
    assert settings.ws_top_n == 1
    assert settings.ws_stale_seconds == 600


def test_cookie_secure_config(monkeypatch):
    monkeypatch.setenv("DEMO_COOKIE_SECURE", "true")
    assert get_settings().cookie_secure is True

    monkeypatch.setenv("DEMO_COOKIE_SECURE", "0")
    assert get_settings().cookie_secure is False


def test_translation_config_defaults_and_bounds(monkeypatch):
    for name in [
        "DEMO_TRANSLATION_ENABLED",
        "DEMO_TRANSLATION_PROVIDER",
        "DEMO_TRANSLATION_TARGET_LANGUAGE",
        "DEMO_TRANSLATION_MAX_CHARS",
    ]:
        monkeypatch.delenv(name, raising=False)
    settings = get_settings()
    assert settings.translation_enabled is False
    assert settings.translation_provider == "noop"
    assert settings.translation_target_language == "ja"
    assert settings.translation_max_chars == 4000
    assert settings.translation_model == "Helsinki-NLP/opus-mt-en-jap"
    assert settings.translation_device == "auto"
    assert settings.translation_batch_size == 4
    assert settings.translation_local_files_only is False
    assert settings.azure_translator_endpoint == "https://api.cognitive.microsofttranslator.com"
    assert settings.azure_translator_api_version == "3.0"
    assert settings.azure_translator_source_language == "en"
    assert settings.azure_translator_target_language == "ja"
    assert settings.azure_translator_timeout_seconds == 15
    assert settings.azure_translator_max_retries == 3
    assert settings.azure_translator_batch_size == 20
    monkeypatch.setenv("DEMO_TRANSLATION_MAX_CHARS", "1")
    assert get_settings().translation_max_chars == 200
    monkeypatch.setenv("DEMO_TRANSLATION_MAX_CHARS", "999999")
    assert get_settings().translation_max_chars == 20000
