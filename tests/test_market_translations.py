from dataclasses import replace
import importlib.util
from pathlib import Path

from app.config import Settings
from app.storage import init_db, list_market_translation_attempts
from app.translation import (
    TRANSLATION_FAILED,
    TRANSLATION_SUCCESS,
    NoopTranslator,
    TranslationPayload,
    TranslationUnavailableError,
    add_translation_display,
    add_translation_displays,
    get_market_translation,
    source_hash,
    upsert_market_translation,
)


def _translator_script():
    path = Path("scripts/translate_markets.py")
    spec = importlib.util.spec_from_file_location("translate_markets", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeTranslator:
    provider = "fake"
    model = "fake-ja-v1"

    def __init__(self):
        self.calls = 0

    def translate(self, *, title, question, description, target_language):
        self.calls += 1
        return TranslationPayload(
            title=self._translate(title),
            question=self._translate(question),
            description=self._translate(description) if description else None,
            provider=self.provider,
            model=self.model,
        )

    @staticmethod
    def _translate(value):
        translated = f"訳: {value}"
        lowered = value.lower()
        if "on or before" in lowered:
            translated += " 以前"
        elif "before" in lowered:
            translated += " より前"
        elif "by" in lowered:
            translated += " までに"
        if "more than" in lowered:
            translated += " を超える"
        if "less than" in lowered:
            translated += " 未満"
        if "at least" in lowered:
            translated += " 少なくとも"
        if "only if" in lowered:
            translated += " 場合に限り"
        return translated


class FailingTranslator(FakeTranslator):
    def translate(self, **kwargs):
        self.calls += 1
        raise RuntimeError("secret-token-must-not-be-stored")


def _save_translation(conn, market, title="日本語タイトル", question="日本語質問", description="日本語説明"):
    upsert_market_translation(
        conn,
        market=market,
        language="ja",
        translated_title=title,
        translated_question=question,
        translated_description=description,
        provider="fake",
        model="fake-ja-v1",
        status=TRANSLATION_SUCCESS,
    )


def test_translation_schema_is_reentrant_and_upserts(db_conn, sample_markets):
    init_db(db_conn)
    columns = {row["name"] for row in db_conn.execute("pragma table_info(market_translations)").fetchall()}
    assert {
        "market_id", "language", "translated_title", "translated_question", "translated_description",
        "source_title_hash", "source_question_hash", "source_description_hash",
        "translation_provider", "translation_model", "translation_status", "translated_at", "error_message",
    }.issubset(columns)
    market = sample_markets[0]
    _save_translation(db_conn, market, title="初回")
    _save_translation(db_conn, market, title="更新後")
    row = get_market_translation(db_conn, market["market_id"], "ja")
    assert row["translated_title"] == "更新後"
    assert row["translation_status"] == TRANSLATION_SUCCESS
    assert row["source_title_hash"] == source_hash(market["title"])


def test_source_hash_normalizes_whitespace_and_tracks_each_source_field(sample_markets):
    assert source_hash("\r\n hello \r\nworld \n") == source_hash("hello\nworld")
    assert source_hash("") == source_hash(None)
    assert source_hash("title") != source_hash("title changed")
    market = sample_markets[0]
    assert source_hash(market["title"]) != source_hash(market["question"])
    assert source_hash(market["description"]) != source_hash(market["description"] + " changed")


def test_translation_cache_is_used_only_for_matching_enabled_japanese_source(db_conn, sample_markets):
    market = sample_markets[0]
    _save_translation(db_conn, market)
    translated = add_translation_display(db_conn, market, language="ja", enabled=True)
    assert translated["display_title"] == "日本語タイトル"
    assert translated["translation_available"] is True
    changed = dict(market, title="changed source")
    stale = add_translation_display(db_conn, changed, language="ja", enabled=True)
    assert stale["display_title"] == "changed source"
    assert stale["translation_available"] is False
    disabled = add_translation_display(db_conn, market, language="ja", enabled=False)
    english = add_translation_display(db_conn, market, language="en", enabled=True)
    assert disabled["display_title"] == market["title"]
    assert english["display_title"] == market["title"]


def test_failed_translation_falls_back_to_original(db_conn, sample_markets):
    market = sample_markets[0]
    upsert_market_translation(
        db_conn,
        market=market,
        language="ja",
        translated_title="should not display",
        translated_question="should not display",
        translated_description="should not display",
        provider="fake",
        model="fake",
        status=TRANSLATION_FAILED,
        error_message="translation failed",
    )
    display = add_translation_display(db_conn, market, language="ja", enabled=True)
    assert display["display_title"] == market["title"]
    assert display["translation_available"] is False


def test_translation_list_display_uses_saved_rows_without_changing_raw_markets(db_conn, sample_markets):
    _save_translation(db_conn, sample_markets[0], title="一覧日本語")
    rendered = add_translation_displays(db_conn, sample_markets, language="ja", enabled=True)
    assert rendered[0]["display_title"] == "一覧日本語"
    assert rendered[0]["title"] == sample_markets[0]["title"]


def test_japanese_views_use_saved_translation_and_english_uses_original(client, db_conn, sample_markets, monkeypatch):
    import app.main as main

    market = sample_markets[0]
    _save_translation(db_conn, market, title="保存済み日本語タイトル", question="保存済み日本語質問", description="保存済み日本語説明")
    monkeypatch.setattr(main, "settings", replace(main.settings, translation_enabled=True))
    assert "保存済み日本語タイトル" in client.get("/?lang=ja").text
    assert "保存済み日本語タイトル" in client.get("/markets?lang=ja").text
    detail = client.get(f"/markets/{market['market_id']}?lang=ja").text
    assert "保存済み日本語タイトル" in detail
    assert market["title"] in detail
    assert "この日本語文は機械翻訳です。正式な判定条件は原文をご確認ください。" in detail
    assert 'aria-selected="true"' in detail
    assert 'data-translation-panel="original" hidden' in detail
    assert "<noscript>" in detail
    english = client.get(f"/markets/{market['market_id']}?lang=en").text
    assert market["title"] in english
    assert "保存済み日本語タイトル" not in english


def test_japanese_view_without_translation_keeps_original(client, sample_markets):
    market = sample_markets[0]
    html = client.get(f"/markets/{market['market_id']}?lang=ja").text
    assert market["title"] in html
    assert "data-translation-switch" not in html


def test_translation_xss_is_escaped_and_lightweight_update_does_not_replace_text(client, db_conn, sample_markets, monkeypatch):
    import app.main as main

    market = sample_markets[0]
    _save_translation(db_conn, market, title='<img src=x onerror="alert(1)">')
    monkeypatch.setattr(main, "settings", replace(main.settings, translation_enabled=True))
    html = client.get(f"/markets/{market['market_id']}?lang=ja").text
    assert '<img src=x onerror="alert(1)">' not in html
    assert "&lt;img" in html
    live = client.get(f"/api/markets/{market['market_id']}/live").json()
    assert "display_title" not in live
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "display_title" not in script
    assert "applyLiveMarketUpdate" in script


def test_translation_cli_limit_dry_run_cache_force_and_retry(db_conn, sample_markets, capsys):
    module = _translator_script()
    settings = Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", translation_enabled=True)
    fake = FakeTranslator()
    assert module.run(["--limit", "1", "--dry-run"], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 0
    assert get_market_translation(db_conn, sample_markets[0]["market_id"], "ja") is None
    assert module.run(["--limit", "1"], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 1
    assert module.run(["--limit", "1"], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 1
    assert module.run(["--limit", "1", "--force"], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 2
    assert module.run(["--limit", "1", "--force", "--include-description"], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 3
    market = sample_markets[1]
    assert module.run(["--market-id", market["market_id"]], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 4
    row = db_conn.execute(
        "select translated_description from market_translations where translated_description is not null limit 1"
    ).fetchone()
    assert row["translated_description"].startswith("訳:")
    assert "translation summary:" in capsys.readouterr().out


def test_translation_cli_noop_and_failures_are_not_success_or_secret(db_conn, sample_markets):
    module = _translator_script()
    settings = Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:", translation_enabled=True)
    market = sample_markets[0]
    assert module.run(["--market-id", market["market_id"]], conn=db_conn, settings=settings, translator=NoopTranslator()) == 0
    row = get_market_translation(db_conn, market["market_id"], "ja")
    assert row is None
    failing = FailingTranslator()
    assert module.run(["--market-id", sample_markets[1]["market_id"], "--force", "--fail-on-error"], conn=db_conn, settings=settings, translator=failing) == 1
    row = get_market_translation(db_conn, sample_markets[1]["market_id"], "ja")
    assert row is None
    attempt = db_conn.execute("select quality_details_json from market_translation_attempts order by id desc limit 1").fetchone()
    assert "secret-token" not in attempt["quality_details_json"]


def test_translation_cli_dry_run_json_does_not_initialize_provider_or_write(db_conn, sample_markets, capsys):
    module = _translator_script()
    settings = Settings(live=False, poll_seconds=30, limit=1, db_path=":memory:", translation_provider="azure")
    assert module.run(["--dry-run", "--json"], conn=db_conn, settings=settings) == 0
    result = __import__("json").loads(capsys.readouterr().out)
    assert result["selected"] == 1
    assert result["attempts_recorded"] == 0
    assert get_market_translation(db_conn, sample_markets[0]["market_id"], "ja") is None
    assert list_market_translation_attempts(db_conn) == []


def test_translation_cli_quality_failure_records_field_details_without_overwriting_cache(db_conn, sample_markets, capsys):
    module = _translator_script()
    settings = Settings(live=False, poll_seconds=30, limit=1, db_path=":memory:", translation_enabled=True)
    market = sample_markets[0]
    _save_translation(db_conn, market, title="既存合格", question="既存質問", description="既存説明")

    class BadTranslator(FakeTranslator):
        def translate(self, *, title, question, description, target_language):
            self.calls += 1
            return TranslationPayload("", "", None, provider=self.provider, model=self.model)

    assert module.run(["--market-id", market["market_id"], "--force", "--json", "--fail-on-error"], conn=db_conn, settings=settings, translator=BadTranslator()) == 1
    result = __import__("json").loads(capsys.readouterr().out)
    item = result["markets"][0]
    assert item["status"] == "failed"
    assert item["cache_updated"] is False
    assert any(code.startswith("title:") for code in item["failure_codes"])
    assert get_market_translation(db_conn, market["market_id"], "ja")["translated_title"] == "既存合格"
    attempt = list_market_translation_attempts(db_conn, market_id=market["market_id"])[0]
    assert attempt["quality_details"]["fields"]["title"]["status"] == "failed"
    assert attempt["latency_ms"] >= 0
    assert attempt["metered_characters"] == sum(len(str(market.get(name) or "")) for name in ("title", "question", "description"))


def test_translation_cli_retranslates_when_cache_provider_model_or_source_changes(db_conn, sample_markets):
    module = _translator_script()
    settings = Settings(live=False, poll_seconds=30, limit=1, db_path=":memory:", translation_enabled=True)
    fake = FakeTranslator()
    market = sample_markets[0]
    assert module.run(["--market-id", market["market_id"]], conn=db_conn, settings=settings, translator=fake) == 0
    assert module.run(["--market-id", market["market_id"]], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 1
    changed = dict(market, title=f"{market['title']} changed")
    db_conn.execute("update markets set payload = ? where market_id = ?", (__import__("json").dumps(changed), market["market_id"]))
    db_conn.commit()
    assert module.run(["--market-id", market["market_id"]], conn=db_conn, settings=settings, translator=fake) == 0
    assert fake.calls == 2
    other_model = FakeTranslator()
    other_model.model = "fake-ja-v2"
    assert module.run(["--market-id", market["market_id"]], conn=db_conn, settings=settings, translator=other_model) == 0
    assert other_model.calls == 1


def test_translation_cli_rejects_invalid_arguments_missing_key_and_unknown_market(db_conn, sample_markets, capsys):
    module = _translator_script()
    azure = Settings(live=False, poll_seconds=30, limit=1, db_path=":memory:", translation_provider="azure")
    assert module.run(["--limit", "0"], conn=db_conn, settings=azure) == 2
    assert module.run([], conn=db_conn, settings=azure) == 2
    assert module.run(["--market-id", "missing"], conn=db_conn, settings=Settings(live=False, poll_seconds=30, limit=1, db_path=":memory:")) == 2
    assert "Azure Translator key is not configured" in capsys.readouterr().err


def test_translation_cli_batch_mapping_and_auth_failure_attempts_are_safe(db_conn, sample_markets, capsys):
    module = _translator_script()
    settings = Settings(live=False, poll_seconds=30, limit=2, db_path=":memory:", translation_enabled=True)

    class BatchTranslator(FakeTranslator):
        def translate_batch(self, requests):
            self.calls += 1
            return [
                TranslationPayload(self._translate(title), self._translate(question), self._translate(description) if description else None, self.provider, self.model)
                for title, question, description, _ in requests
            ]

    batch = BatchTranslator()
    assert module.run(["--limit", "2"], conn=db_conn, settings=settings, translator=batch) == 0
    assert batch.calls == 1
    attempts = list_market_translation_attempts(db_conn)
    assert len(attempts) == 2
    assert all(item["quality_status"] == "passed" for item in attempts)

    class AuthFailure:
        provider = "azure"
        model = "azure-translator-3.0"

        def __init__(self):
            self.calls = 0

        def translate_batch(self, requests):
            self.calls += 1
            raise TranslationUnavailableError("Azure Translator returned HTTP 401")

    auth = AuthFailure()
    assert module.run(["--limit", "2", "--force", "--json"], conn=db_conn, settings=settings, translator=auth) == 2
    output = capsys.readouterr()
    assert auth.calls == 1
    assert "secret" not in output.out + output.err
    assert len(list_market_translation_attempts(db_conn)) == 4
