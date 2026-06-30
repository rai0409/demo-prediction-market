import asyncio

import httpx
import pytest

from app.config import Settings
from app.polymarket_gamma import load_markets
from app.storage import connect, init_db, store_markets


@pytest.fixture()
def sample_markets():
    return load_markets(live=False, limit=50)


@pytest.fixture()
def db_conn(sample_markets):
    conn = connect(":memory:")
    init_db(conn)
    store_markets(conn, sample_markets)
    yield conn
    conn.close()


@pytest.fixture()
def client(db_conn, monkeypatch):
    import app.main as main

    monkeypatch.setattr(main, "db", db_conn)
    monkeypatch.setattr(
        main,
        "settings",
        Settings(live=False, poll_seconds=30, limit=50, db_path=":memory:"),
    )
    async def override_conn():
        return db_conn

    main.app.dependency_overrides[main.get_conn] = override_conn

    class ASGITestClient:
        def request(self, method, url, **kwargs):
            async def do_request():
                transport = httpx.ASGITransport(app=main.app)
                async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
                    return await test_client.request(method, url, **kwargs)

            return asyncio.run(do_request())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    yield ASGITestClient()
    main.app.dependency_overrides.clear()
