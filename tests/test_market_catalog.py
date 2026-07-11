from pathlib import Path
import re

from app.storage import list_market_catalog, replace_markets


def _catalog_markets(sample_markets, count=25):
    markets = []
    for index in range(count):
        market = dict(sample_markets[index % len(sample_markets)])
        market.update(
            {
                "market_id": f"catalog-internal-{index}",
                "slug": f"catalog-slug-{index}",
                "title": f"Catalog title {index}",
                "question": f"Catalog question {index}",
                "active": True,
                "closed": False,
                "end_date": f"2099-12-{(index % 28) + 1:02d}T00:00:00+00:00",
                "fetched_at": f"2026-01-{index + 1:02d}T00:00:00+00:00",
                "volume_24hr": float(index),
                "liquidity": float(count - index),
                "probabilities": {"YES": (index + 1) / (count + 1), "NO": 1 - ((index + 1) / (count + 1))},
            }
        )
        markets.append(market)
    return markets


def test_market_catalog_renders_ja_en_and_product_navigation(client):
    japanese = client.get("/markets")
    english = client.get("/markets?lang=en")
    assert japanese.status_code == 200
    assert "全マーケット" in japanese.text
    assert english.status_code == 200
    assert "All markets" in english.text
    assert 'href="/markets?lang=ja"' in client.get("/?lang=ja").text
    assert 'href="/api/markets"' not in japanese.text


def test_market_catalog_defaults_to_active_and_handles_status_filters(client, db_conn, sample_markets):
    active = dict(sample_markets[0], market_id="active-catalog", title="Active catalog")
    closed = dict(sample_markets[1], market_id="closed-catalog", title="Closed catalog", active=False, closed=True)
    replace_markets(db_conn, [active, closed])
    assert "Active catalog" in client.get("/markets").text
    assert "Closed catalog" not in client.get("/markets").text
    assert "Closed catalog" in client.get("/markets?status=closed").text
    assert "Active catalog" in client.get("/markets?status=all").text
    fallback = client.get("/markets?status=not-a-status").text
    assert 'value="active" selected' in fallback
    assert "Closed catalog" not in fallback


def test_market_catalog_searches_title_question_slug_case_insensitively(client, db_conn, sample_markets):
    markets = _catalog_markets(sample_markets, 3)
    markets[0].update(title="NATO title", question="other question", slug="title-match")
    markets[1].update(title="other title", question="Nato question", slug="question-match")
    markets[2].update(title="Slug match title", question="other question", slug="NATO-slug")
    replace_markets(db_conn, markets)
    assert "NATO title" in client.get("/markets?q=nato").text
    assert "Nato question" in client.get("/markets?q=NATO").text
    assert "Slug match title" in client.get("/markets?q=nato-slug").text
    assert "条件に一致するマーケットはありません" in client.get("/markets?q=no-such-market").text


def test_market_catalog_search_is_escaped_and_sql_safe(client, db_conn, sample_markets):
    replace_markets(db_conn, _catalog_markets(sample_markets, 3))
    injection = "%') OR 1=1 --"
    response = client.get("/markets", params={"q": injection})
    assert response.status_code == 200
    assert client.get("/markets?q=Catalog").status_code == 200
    xss = '<img src=x onerror="alert(1)">'
    response = client.get("/markets", params={"q": xss})
    assert response.status_code == 200
    assert xss not in response.text
    assert "&lt;img" in response.text


def test_market_catalog_sorts_with_sql_allowlists(db_conn, sample_markets):
    replace_markets(db_conn, _catalog_markets(sample_markets, 3))
    desc = list_market_catalog(db_conn, "", "active", "volume_24h", "desc", 10, 0)["markets"]
    asc = list_market_catalog(db_conn, "", "active", "volume_24h", "asc", 10, 0)["markets"]
    liquidity = list_market_catalog(db_conn, "", "active", "liquidity", "desc", 10, 0)["markets"]
    end_date = list_market_catalog(db_conn, "", "active", "end_date", "asc", 10, 0)["markets"]
    probability = list_market_catalog(db_conn, "", "active", "probability", "desc", 10, 0)["markets"]
    updated = list_market_catalog(db_conn, "", "active", "updated", "desc", 10, 0)["markets"]
    assert [market["volume_24hr"] for market in desc] == [2.0, 1.0, 0.0]
    assert [market["volume_24hr"] for market in asc] == [0.0, 1.0, 2.0]
    assert [market["liquidity"] for market in liquidity] == [3.0, 2.0, 1.0]
    assert [market["end_date"] for market in end_date] == sorted(market["end_date"] for market in end_date)
    assert [market["probabilities"]["YES"] for market in probability] == [0.75, 0.5, 0.25]
    assert [market["fetched_at"] for market in updated] == ["2026-01-03T00:00:00+00:00", "2026-01-02T00:00:00+00:00", "2026-01-01T00:00:00+00:00"]
    fallback = list_market_catalog(db_conn, "", "active", "not-a-column", "not-an-order", 10, 0)["markets"]
    assert [market["volume_24hr"] for market in fallback] == [2.0, 1.0, 0.0]


def test_market_catalog_paginates_and_preserves_filters(client, db_conn, sample_markets):
    replace_markets(db_conn, _catalog_markets(sample_markets, 25))
    first_page = client.get("/markets?q=Catalog&sort=liquidity&order=desc&page_size=10&page=1")
    assert first_page.status_code == 200
    assert first_page.text.count('class="market-card"') == 10
    assert "1 / 3 ページ" in first_page.text
    assert "q=Catalog" in first_page.text
    assert "sort=liquidity" in first_page.text
    assert "page=2" in first_page.text
    assert client.get("/markets?page_size=20").text.count('class="market-card"') == 20
    assert client.get("/markets?page_size=50").text.count('class="market-card"') == 25
    assert client.get("/markets?page_size=999").text.count('class="market-card"') == 20
    assert "1 / 3 ページ" in client.get("/markets?page=0&page_size=10").text
    assert "1 / 3 ページ" in client.get("/markets?page=-1&page_size=10").text
    assert "3 / 3 ページ" in client.get("/markets?page=999&page_size=10").text


def test_market_catalog_hides_internal_values_and_uses_targeted_updates(client, db_conn, sample_markets):
    markets = _catalog_markets(sample_markets, 1)
    markets[0]["clob_token_ids"] = ["sensitive-token-id"]
    replace_markets(db_conn, markets)
    html = client.get("/markets").text
    assert 'data-market-id="catalog-internal-0"' in html
    visible = re.sub(r"<script\\b[^>]*>.*?</script>", "", html, flags=re.DOTALL | re.IGNORECASE)
    visible = re.sub(r"<[^>]+>", "", visible)
    for value in ["catalog-internal-0", "sensitive-token-id", "REST", "WebSocket", "polling", "data_source_status", "realtime_status"]:
        assert value not in visible
    assert 'data-live-field="volume_24hr"' in html
    assert 'data-live-probability-bar=' in html
    script = Path("app/static/app.js").read_text(encoding="utf-8")
    assert "pollVisibleMarketCards" in script
    assert 'fetch("/api/markets")' not in script
