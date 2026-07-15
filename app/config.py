from dataclasses import dataclass
import os


@dataclass(frozen=True)
class Settings:
    live: bool
    poll_seconds: int
    limit: int
    db_path: str
    auto_refresh: bool = False
    refresh_seconds: int = 60
    quick_refresh_seconds: int = 5
    detail_refresh_seconds: int = 3
    ws_enabled: bool = False
    ws_top_n: int = 10
    ws_stale_seconds: int = 90
    admin_token: str = ""
    max_demo_stake: float = 10000.0
    cookie_secure: bool = False
    strict_participant_access: bool = False
    participant_codes: str = ""
    session_cookie_name: str = "demo_user_id"
    participant_switch_enabled: bool = False
    allow_demo_user_header: bool = False
    auth_session_cookie_name: str = "auth_session"
    auth_session_ttl_seconds: int = 60 * 60 * 24 * 7
    auth_cookie_secure: bool = False
    auth_cookie_samesite: str = "lax"
    auth_cookie_path: str = "/"
    auth_registration_enabled: bool = True
    auth_password_min_length: int = 12
    auth_password_max_length: int = 256
    auth_login_rate_limit: int = 5
    auth_login_rate_window_seconds: int = 300
    translation_enabled: bool = False
    translation_provider: str = "noop"
    translation_target_language: str = "ja"
    translation_max_chars: int = 4000
    translation_model: str = "Helsinki-NLP/opus-mt-en-jap"
    translation_device: str = "auto"
    translation_batch_size: int = 4
    translation_local_files_only: bool = False
    azure_translator_key: str = ""
    azure_translator_endpoint: str = "https://api.cognitive.microsofttranslator.com"
    azure_translator_region: str = ""
    azure_translator_api_version: str = "3.0"
    azure_translator_source_language: str = "en"
    azure_translator_target_language: str = "ja"
    azure_translator_timeout_seconds: int = 15
    azure_translator_max_retries: int = 3
    azure_translator_batch_size: int = 20


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


def _auth_int_env(name: str, default: int, *, minimum: int, maximum: int) -> int:
    value = os.getenv(name)
    if value is None or value == "":
        return default
    try:
        parsed = int(value)
    except ValueError as exc:
        raise ValueError(f"{name} must be an integer") from exc
    if not minimum <= parsed <= maximum:
        raise ValueError(f"{name} is outside its allowed range")
    return parsed


def get_settings() -> Settings:
    auth_cookie_name = os.getenv("AUTH_SESSION_COOKIE_NAME", "auth_session").strip()
    if not auth_cookie_name:
        raise ValueError("AUTH_SESSION_COOKIE_NAME must not be empty")
    demo_cookie_name = os.getenv("DEMO_SESSION_COOKIE_NAME", "demo_user_id").strip() or "demo_user_id"
    if auth_cookie_name == demo_cookie_name:
        raise ValueError("AUTH_SESSION_COOKIE_NAME must differ from DEMO_SESSION_COOKIE_NAME")
    auth_ttl = _auth_int_env("AUTH_SESSION_TTL_SECONDS", 60 * 60 * 24 * 7, minimum=1, maximum=60 * 60 * 24 * 31)
    password_min = _auth_int_env("AUTH_PASSWORD_MIN_LENGTH", 12, minimum=1, maximum=1024)
    password_max = _auth_int_env("AUTH_PASSWORD_MAX_LENGTH", 256, minimum=1, maximum=4096)
    if password_min > password_max:
        raise ValueError("AUTH_PASSWORD_MIN_LENGTH must not exceed AUTH_PASSWORD_MAX_LENGTH")
    auth_samesite = os.getenv("AUTH_COOKIE_SAMESITE", "lax").strip().lower()
    if auth_samesite not in {"lax", "strict"}:
        raise ValueError("AUTH_COOKIE_SAMESITE must be lax or strict")
    auth_secure = _bool_env("AUTH_COOKIE_SECURE", False)
    auth_path = os.getenv("AUTH_COOKIE_PATH", "/").strip()
    if not auth_path.startswith("/"):
        raise ValueError("AUTH_COOKIE_PATH must start with /")
    live = _bool_env("DEMO_PREDICTION_LIVE", False)
    production = _bool_env("DEMO_PREDICTION_PRODUCTION", False)
    if production and not auth_secure:
        raise ValueError("AUTH_COOKIE_SECURE must be enabled in live mode")
    return Settings(
        live=live,
        poll_seconds=max(15, min(30, _int_env("DEMO_PREDICTION_POLL_SECONDS", 30))),
        limit=max(1, _int_env("DEMO_PREDICTION_LIMIT", 50)),
        db_path=os.getenv("DEMO_PREDICTION_DB", "data/demo_prediction.sqlite3"),
        auto_refresh=_bool_env("DEMO_PREDICTION_AUTO_REFRESH", False),
        refresh_seconds=max(30, min(300, _int_env("DEMO_PREDICTION_REFRESH_SECONDS", 60))),
        quick_refresh_seconds=max(3, min(30, _int_env("DEMO_PREDICTION_QUICK_REFRESH_SECONDS", 5))),
        detail_refresh_seconds=max(2, min(15, _int_env("DEMO_PREDICTION_DETAIL_REFRESH_SECONDS", 3))),
        ws_enabled=_bool_env("DEMO_PREDICTION_WS_ENABLED", False),
        ws_top_n=max(1, min(50, _int_env("DEMO_PREDICTION_WS_TOP_N", 10))),
        ws_stale_seconds=max(15, min(600, _int_env("DEMO_PREDICTION_WS_STALE_SECONDS", 90))),
        admin_token=os.getenv("DEMO_ADMIN_TOKEN", "").strip(),
        max_demo_stake=float(max(1, _int_env("DEMO_PREDICTION_MAX_DEMO_STAKE", 10000))),
        cookie_secure=_bool_env("DEMO_COOKIE_SECURE", False),
        strict_participant_access=_bool_env("DEMO_STRICT_PARTICIPANT_ACCESS", False),
        participant_codes=os.getenv("DEMO_PARTICIPANT_CODES", "").strip(),
        session_cookie_name=demo_cookie_name,
        participant_switch_enabled=_bool_env("DEMO_PARTICIPANT_SWITCH_ENABLED", False),
        allow_demo_user_header=_bool_env("DEMO_ALLOW_USER_HEADER", False),
        auth_session_cookie_name=auth_cookie_name,
        auth_session_ttl_seconds=auth_ttl,
        auth_cookie_secure=auth_secure,
        auth_cookie_samesite=auth_samesite,
        auth_cookie_path=auth_path,
        auth_registration_enabled=_bool_env("AUTH_REGISTRATION_ENABLED", True),
        auth_password_min_length=password_min,
        auth_password_max_length=password_max,
        auth_login_rate_limit=_auth_int_env("AUTH_LOGIN_RATE_LIMIT", 5, minimum=1, maximum=100),
        auth_login_rate_window_seconds=_auth_int_env("AUTH_LOGIN_RATE_WINDOW_SECONDS", 300, minimum=1, maximum=86400),
        translation_enabled=_bool_env("DEMO_TRANSLATION_ENABLED", False),
        translation_provider=os.getenv("DEMO_TRANSLATION_PROVIDER", "noop").strip().lower() or "noop",
        translation_target_language=os.getenv("DEMO_TRANSLATION_TARGET_LANGUAGE", "ja").strip().lower() or "ja",
        translation_max_chars=max(200, min(20000, _int_env("DEMO_TRANSLATION_MAX_CHARS", 4000))),
        translation_model=os.getenv("DEMO_TRANSLATION_MODEL", "Helsinki-NLP/opus-mt-en-jap").strip() or "Helsinki-NLP/opus-mt-en-jap",
        translation_device=(os.getenv("DEMO_TRANSLATION_DEVICE", "auto").strip().lower() or "auto"),
        translation_batch_size=max(1, min(32, _int_env("DEMO_TRANSLATION_BATCH_SIZE", 4))),
        translation_local_files_only=_bool_env("DEMO_TRANSLATION_LOCAL_FILES_ONLY", False),
        azure_translator_key=os.getenv("AZURE_TRANSLATOR_KEY", "").strip(),
        azure_translator_endpoint=(os.getenv("AZURE_TRANSLATOR_ENDPOINT", "https://api.cognitive.microsofttranslator.com").strip() or "https://api.cognitive.microsofttranslator.com"),
        azure_translator_region=os.getenv("AZURE_TRANSLATOR_REGION", "").strip(),
        azure_translator_api_version=(os.getenv("AZURE_TRANSLATOR_API_VERSION", "3.0").strip() or "3.0"),
        azure_translator_source_language=(os.getenv("AZURE_TRANSLATOR_SOURCE_LANGUAGE", "en").strip().lower() or "en"),
        azure_translator_target_language=(os.getenv("AZURE_TRANSLATOR_TARGET_LANGUAGE", "ja").strip().lower() or "ja"),
        azure_translator_timeout_seconds=max(1, min(120, _int_env("AZURE_TRANSLATOR_TIMEOUT_SECONDS", 15))),
        azure_translator_max_retries=max(0, min(10, _int_env("AZURE_TRANSLATOR_MAX_RETRIES", 3))),
        azure_translator_batch_size=max(1, min(100, _int_env("AZURE_TRANSLATOR_BATCH_SIZE", 20))),
    )
