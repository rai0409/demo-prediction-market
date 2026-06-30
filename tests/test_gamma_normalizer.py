from app.polymarket_gamma import load_sample_events, normalize_events


def test_sample_fallback_loading():
    events = load_sample_events()
    assert len(events) >= 3


def test_market_normalization(sample_markets):
    market = sample_markets[0]
    assert market["market_id"]
    assert market["source"] == "sample"
    assert market["data_source_status"] == "sample_fallback"
    assert "YES" in market["outcomes"]
    assert "NO" in market["outcomes"]
    assert 0 <= market["probabilities"]["YES"] <= 1


def test_live_style_normalization_defensive_parsing():
    events = [
        {
            "id": "event-a",
            "title": "Event A",
            "markets": [
                {
                    "id": "market-a",
                    "question": "Will A happen?",
                    "outcomes": "[\"YES\", \"NO\"]",
                    "outcomePrices": "[\"0.25\", \"0.75\"]",
                    "volume24hr": "12.5",
                }
            ],
        }
    ]
    markets = normalize_events(events, source="polymarket", status="live", fetched_at="now")
    assert markets[0]["market_id"] == "market-a"
    assert markets[0]["probabilities"]["NO"] == 0.75
    assert markets[0]["volume_24hr"] == 12.5


def test_normalizer_handles_object_with_events():
    payload = {
        "events": [
            {
                "id": "event-b",
                "title": "Event B",
                "markets": [{"id": "market-b", "question": "Will B happen?", "outcomes": ["YES", "NO"]}],
            }
        ]
    }
    markets = normalize_events(payload, source="polymarket", status="live", fetched_at="now")
    assert markets[0]["market_id"] == "market-b"


def test_normalizer_handles_object_with_data():
    payload = {
        "data": [
            {
                "id": "event-c",
                "title": "Event C",
                "markets": [{"id": "market-c", "question": "Will C happen?", "outcomes": ["YES", "NO"]}],
            }
        ]
    }
    markets = normalize_events(payload, source="polymarket", status="live", fetched_at="now")
    assert markets[0]["market_id"] == "market-c"


def test_normalizer_handles_market_object_directly():
    payload = {
        "id": "market-direct",
        "question": "Will direct market display?",
        "outcomes": ["YES", "NO"],
        "outcomePrices": [0.4, 0.6],
    }
    markets = normalize_events(payload, source="polymarket", status="live", fetched_at="now")
    assert markets[0]["market_id"] == "market-direct"
    assert markets[0]["probabilities"]["YES"] == 0.4


def test_outcome_prices_as_json_string():
    payload = [
        {
            "id": "market-json-prices",
            "question": "Will JSON prices parse?",
            "outcomes": "[\"YES\", \"NO\"]",
            "outcomePrices": "[0.31, 0.69]",
            "clobTokenIds": "[\"token-a\", \"token-b\"]",
        }
    ]
    markets = normalize_events(payload, source="polymarket", status="live", fetched_at="now")
    assert markets[0]["probabilities"]["NO"] == 0.69


def test_missing_optional_fields_do_not_crash():
    payload = [{"id": "market-minimal", "question": "Will minimal market display?", "outcomes": ["YES", "NO"]}]
    markets = normalize_events(payload, source="polymarket", status="live", fetched_at="now")
    assert markets[0]["volume"] == 0
    assert markets[0]["volume_24hr"] == 0
    assert markets[0]["liquidity"] == 0
    assert markets[0]["end_date"] == ""
