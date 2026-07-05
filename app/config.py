from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    live: bool
    poll_seconds: int
    limit: int
    db_path: str
    auto_refresh: bool = False
    refresh_seconds: int = 30
    ws_enabled: bool = False
    ws_top_n: int = 10
    ws_stale_seconds: int = 90
    admin_token: str = ""
    max_demo_stake: float = 10000.0


def _int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except ValueError:
        return default


def _bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off", ""}:
        return False
    return default


def get_settings() -> Settings:
    return Settings(
        live=_bool_env("DEMO_PREDICTION_LIVE", False),
        poll_seconds=max(15, min(30, _int_env("DEMO_PREDICTION_POLL_SECONDS", 30))),
        limit=max(1, _int_env("DEMO_PREDICTION_LIMIT", 50)),
        db_path=os.getenv("DEMO_PREDICTION_DB", "data/demo_prediction.sqlite3"),
        auto_refresh=_bool_env("DEMO_PREDICTION_AUTO_REFRESH", False),
        refresh_seconds=max(15, min(300, _int_env("DEMO_PREDICTION_REFRESH_SECONDS", 30))),
        ws_enabled=_bool_env("DEMO_PREDICTION_WS_ENABLED", False),
        ws_top_n=max(1, min(50, _int_env("DEMO_PREDICTION_WS_TOP_N", 10))),
        ws_stale_seconds=max(15, min(600, _int_env("DEMO_PREDICTION_WS_STALE_SECONDS", 90))),
        admin_token=os.getenv("DEMO_ADMIN_TOKEN", "").strip(),
        max_demo_stake=float(max(1, _int_env("DEMO_PREDICTION_MAX_DEMO_STAKE", 10000))),
    )
