from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import re
import sqlite3
from typing import Any, Protocol

from app.config import Settings


TRANSLATION_SUCCESS = "success"
TRANSLATION_FAILED = "failed"


class TranslationUnavailableError(RuntimeError):
    """Raised when no configured translator can produce a translation."""


@dataclass(frozen=True)
class TranslationPayload:
    title: str
    question: str
    description: str | None = None
    provider: str = "unknown"
    model: str = "unknown"


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


def get_translator(settings: Settings) -> Translator:
    # This release intentionally ships no external provider or model dependency.
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
