from app.storage import get_market, list_markets, list_snapshots, store_markets


def test_snapshot_storage(db_conn, sample_markets):
    market_id = sample_markets[0]["market_id"]
    store_markets(db_conn, sample_markets[:1])
    assert get_market(db_conn, market_id)["market_id"] == market_id
    assert len(list_markets(db_conn)) >= 3
    assert len(list_snapshots(db_conn, market_id)) >= 2
