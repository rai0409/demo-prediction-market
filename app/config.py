from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    live: bool
    poll_seconds: int
    limit: int
    db_path: str


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
    )
