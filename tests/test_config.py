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
    settings = get_settings()
    assert settings.auto_refresh is False
    assert settings.refresh_seconds == 30


def test_refresh_seconds_clamp(monkeypatch):
    monkeypatch.setenv("DEMO_PREDICTION_REFRESH_SECONDS", "5")
    assert get_settings().refresh_seconds == 15
    monkeypatch.setenv("DEMO_PREDICTION_REFRESH_SECONDS", "999")
    assert get_settings().refresh_seconds == 300


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
