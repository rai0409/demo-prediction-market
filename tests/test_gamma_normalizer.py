from app.polymarket_gamma import load_sample_events, normalize_events


def test_sample_fallback_loading():
    events = load_sample_events()
    assert len(events) >= 3


def test_market_normalization(sample_markets):
    market = sample_markets[0]
    assert market["market_id"]
    assert market["source"] == "sample"
    assert market["data_source_status"] == "sample fallback"
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
    markets = normalize_events(events, source="polymarket_gamma", status="live", fetched_at="now")
    assert markets[0]["market_id"] == "market-a"
    assert markets[0]["probabilities"]["NO"] == 0.75
    assert markets[0]["volume_24hr"] == 12.5
