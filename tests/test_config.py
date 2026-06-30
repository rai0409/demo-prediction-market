from app.config import get_settings


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
