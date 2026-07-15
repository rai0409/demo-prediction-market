import asyncio

import httpx
import pytest

from app.config import Settings
from app.polymarket_gamma import load_markets
from app.polymarket_gamma import MarketDetailResult
from app.storage import connect, get_market, init_db, store_markets


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
    import app.settlement as settlement_module

    def fresh_detail(market_id):
        market = dict(get_market(db_conn, market_id) or {})
        market["id"] = market_id
        market["clobTokenIds"] = market.get("clob_token_ids") or [f"{market_id}-yes", f"{market_id}-no"]
        if market.get("closed") and market.get("probabilities", {}).get("YES") == 1.0:
            market["winningOutcome"] = "YES"
        return MarketDetailResult(True, "ok", market, "2026-01-01T00:00:00+00:00", "mock://market")

    monkeypatch.setattr(settlement_module, "fetch_market_detail_for_settlement", fresh_detail)

    monkeypatch.setattr(main, "db", db_conn)
    monkeypatch.setattr(
        main,
        "settings",
        Settings(
            live=False,
            poll_seconds=30,
            limit=50,
            db_path=":memory:",
            admin_token="test-admin",
            participant_switch_enabled=True,
            allow_demo_user_header=True,
        ),
    )
    main._post_rate_events.clear()
    main._auth_failure_events.clear()
    async def override_conn():
        return db_conn

    main.app.dependency_overrides[main.get_conn] = override_conn

    class ASGITestClient:
        def __init__(self):
            self.cookies = {}

        async def _send(self, method, url, **kwargs):
            headers = dict(kwargs.pop("headers", {}) or {})
            if self.cookies and "cookie" not in {key.lower() for key in headers}:
                headers["cookie"] = "; ".join(f"{key}={value}" for key, value in self.cookies.items())
            kwargs["headers"] = headers
            transport = httpx.ASGITransport(app=main.app)
            async with httpx.AsyncClient(transport=transport, base_url="http://testserver") as test_client:
                response = await test_client.request(method, url, **kwargs)
            self.cookies.update(response.cookies)
            return response

        async def _ensure_csrf(self):
            if "demo_csrf" not in self.cookies:
                await self._send("GET", "/")
            return self.cookies.get("demo_csrf")

        def request(self, method, url, **kwargs):
            async def do_request():
                auto_security = kwargs.pop("auto_security", True)
                auto_admin = kwargs.pop("auto_admin", True)
                headers = dict(kwargs.pop("headers", {}) or {})
                endpoint = url.split("?", 1)[0]
                if method.upper() == "POST" and auto_security:
                    csrf = await self._ensure_csrf()
                    headers.setdefault("x-csrf-token", csrf)
                if method.upper() == "POST" and auto_admin and endpoint in {
                    "/api/demo/wallet/add-points",
                    "/api/demo/wallet/reset",
                    "/api/demo/ledger/reversal",
                    "/api/demo/settle",
                    "/api/refresh",
                }:
                    headers.setdefault("x-demo-admin-token", "test-admin")
                kwargs["headers"] = headers
                return await self._send(method, url, **kwargs)

            return asyncio.run(do_request())

        def get(self, url, **kwargs):
            return self.request("GET", url, **kwargs)

        def post(self, url, **kwargs):
            return self.request("POST", url, **kwargs)

    yield ASGITestClient()
    main.app.dependency_overrides.clear()
