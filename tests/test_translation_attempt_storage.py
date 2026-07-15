from __future__ import annotations

import sqlite3

import pytest

import app.storage as storage
from app.storage import (
    connect,
    get_market_translation_attempt,
    init_db,
    insert_market_translation_attempt,
    list_market_translation_attempts,
    record_translation_evaluation,
)
from app.translation import get_market_translation


def _attempt(**overrides):
    values = {
        "market_id": "market-1",
        "target_language": "ja",
        "translation_provider": "azure",
        "translation_model": "translator-v3",
        "source_title_hash": "a" * 64,
        "source_question_hash": "b" * 64,
        "source_description_hash": "c" * 64,
        "translated_title": "日本語タイトル",
        "translated_question": "日本語の質問",
        "translated_description": None,
        "quality_status": "passed",
        "quality_failure_codes": [],
        "quality_details": {"logic": {"passed": True}},
        "latency_ms": 12,
        "metered_characters": 42,
        "provider_request_id": "request-1",
    }
    values.update(overrides)
    return values


def test_attempt_migration_is_additive_reentrant_and_indexed():
    conn = connect(":memory:")
    conn.execute(
        "create table market_translations (market_id text not null, language text not null, translated_title text, "
        "translated_question text, translated_description text, source_title_hash text not null, "
        "source_question_hash text not null, source_description_hash text not null, translation_provider text not null, "
        "translation_model text not null, translation_status text not null, translated_at text not null, error_message text, "
        "primary key (market_id, language))"
    )
    conn.execute(
        "insert into market_translations values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        ("existing", "ja", "既存", None, None, "a", "b", "c", "fake", "v1", "success", "2026-01-01T00:00:00+00:00", None),
    )
    conn.commit()
    init_db(conn)
    init_db(conn)
    assert conn.execute("select translated_title from market_translations where market_id = 'existing'").fetchone()[0] == "既存"
    columns = {row["name"] for row in conn.execute("pragma table_info(market_translation_attempts)")}
    assert {"quality_failure_codes_json", "quality_details_json", "attempted_at"}.issubset(columns)
    indexes = {row["name"] for row in conn.execute("pragma index_list(market_translation_attempts)")}
    assert {
        "idx_market_translation_attempts_market_attempted",
        "idx_market_translation_attempts_quality_attempted",
        "idx_market_translation_attempts_provider_attempted",
    }.issubset(indexes)


def test_insert_get_list_filters_decode_and_order():
    conn = connect(":memory:")
    init_db(conn)
    first = insert_market_translation_attempt(
        conn, **_attempt(quality_status="failed", quality_failure_codes=["z", "a", "z"], attempted_at="2026-01-01T00:00:00+00:00")
    )
    second = insert_market_translation_attempt(
        conn, **_attempt(market_id="market-2", translation_provider="local", attempted_at="2026-01-02T00:00:00+00:00")
    )
    conn.commit()
    restored = get_market_translation_attempt(conn, first["id"])
    assert restored is not None
    assert restored["quality_failure_codes"] == ["z", "a"]
    assert restored["quality_details"] == {"logic": {"passed": True}}
    assert get_market_translation_attempt(conn, 99999) is None
    assert [item["id"] for item in list_market_translation_attempts(conn)] == [second["id"], first["id"]]
    assert [item["id"] for item in list_market_translation_attempts(conn, market_id="market-1")] == [first["id"]]
    assert [item["id"] for item in list_market_translation_attempts(conn, target_language="ja")] == [second["id"], first["id"]]
    assert [item["id"] for item in list_market_translation_attempts(conn, translation_provider="local")] == [second["id"]]
    assert [item["id"] for item in list_market_translation_attempts(conn, quality_status="failed")] == [first["id"]]
    assert len(list_market_translation_attempts(conn, limit=1)) == 1


@pytest.mark.parametrize(
    ("change", "message"),
    [
        ({"market_id": ""}, "market_id"),
        ({"target_language": ""}, "target_language"),
        ({"translation_provider": ""}, "translation_provider"),
        ({"translation_model": ""}, "translation_model"),
        ({"source_title_hash": ""}, "source_title_hash"),
        ({"source_question_hash": ""}, "source_question_hash"),
        ({"source_description_hash": ""}, "source_description_hash"),
        ({"quality_status": "review"}, "quality_status"),
        ({"quality_status": "passed", "quality_failure_codes": ["logic"]}, "passed"),
        ({"quality_status": "failed", "quality_failure_codes": []}, "failed"),
        ({"latency_ms": -1}, "latency_ms"),
        ({"metered_characters": -1}, "metered_characters"),
        ({"quality_details": {"bad": object()}}, "JSON-compatible"),
    ],
)
def test_attempt_validation_rejects_invalid_values(change, message):
    conn = connect(":memory:")
    init_db(conn)
    with pytest.raises(ValueError, match=message):
        insert_market_translation_attempt(conn, **_attempt(**change))


def test_record_evaluation_accepts_only_passed_cache_and_retains_history():
    conn = connect(":memory:")
    init_db(conn)
    passed = record_translation_evaluation(conn, **_attempt(translated_title="採用1"))
    assert passed["accepted"] is True
    assert get_market_translation(conn, "market-1", "ja")["translated_title"] == "採用1"
    failed = record_translation_evaluation(
        conn,
        **_attempt(
            translated_title="不採用",
            quality_status="failed",
            quality_failure_codes=["logic_before_not_preserved"],
        ),
    )
    assert failed["accepted"] is False
    assert get_market_translation(conn, "market-1", "ja")["translated_title"] == "採用1"
    updated = record_translation_evaluation(conn, **_attempt(translated_title="採用2", source_title_hash="d" * 64))
    cache = get_market_translation(conn, "market-1", "ja")
    assert cache["translated_title"] == "採用2"
    assert cache["source_title_hash"] == "d" * 64
    assert len(list_market_translation_attempts(conn, market_id="market-1")) == 3
    assert {row["translation_status"] for row in conn.execute("select * from market_translations")} == {"success"}


def test_record_evaluation_rolls_back_attempt_and_cache_when_cache_upsert_fails(monkeypatch):
    conn = connect(":memory:")
    init_db(conn)
    original = storage._upsert_accepted_market_translation

    def fail(*args, **kwargs):
        raise sqlite3.IntegrityError("cache write failed")

    monkeypatch.setattr(storage, "_upsert_accepted_market_translation", fail)
    with pytest.raises(sqlite3.IntegrityError):
        record_translation_evaluation(conn, **_attempt())
    assert list_market_translation_attempts(conn) == []
    assert get_market_translation(conn, "market-1", "ja") is None
    monkeypatch.setattr(storage, "_upsert_accepted_market_translation", original)


def test_record_evaluation_rolls_back_cache_when_attempt_insert_fails(monkeypatch):
    conn = connect(":memory:")
    init_db(conn)

    def fail(*args, **kwargs):
        raise ValueError("attempt write failed")

    monkeypatch.setattr(storage, "insert_market_translation_attempt", fail)
    with pytest.raises(ValueError, match="attempt write failed"):
        record_translation_evaluation(conn, **_attempt())
    assert get_market_translation(conn, "market-1", "ja") is None


def test_record_evaluation_works_inside_an_outer_transaction():
    conn = connect(":memory:")
    init_db(conn)
    conn.execute("begin")
    record_translation_evaluation(conn, **_attempt())
    assert conn.in_transaction
    conn.rollback()
    assert list_market_translation_attempts(conn) == []
    assert get_market_translation(conn, "market-1", "ja") is None


def test_attempt_schema_does_not_store_secret_transport_fields():
    conn = connect(":memory:")
    init_db(conn)
    columns = {row["name"] for row in conn.execute("pragma table_info(market_translation_attempts)")}
    forbidden = {"api_key", "authorization", "http_headers", "request_body", "response_body"}
    assert not columns & forbidden
