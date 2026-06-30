from pathlib import Path

from app.main import app
from app.safety import DISCLAIMER, FORBIDDEN_ROUTE_PATHS


def test_no_forbidden_routes_exist():
    paths = {route.path for route in app.routes}
    assert paths.isdisjoint(FORBIDDEN_ROUTE_PATHS)


def test_no_forbidden_implementation_terms_in_app_source():
    forbidden = [
        "place_order",
        "create_order",
        "submit_order",
        "wallet_connect",
        "connect_wallet",
        "private_key",
        "seed phrase",
        "mnemonic",
        "CLOBClient",
        "py_clob_client",
    ]
    source_paths = [
        path
        for path in Path("app").rglob("*")
        if path.is_file() and path.suffix in {".py", ".html", ".js", ".css"}
    ]
    source = "\n".join(path.read_text(encoding="utf-8") for path in source_paths)
    for term in forbidden:
        assert term not in source


def test_no_wallet_deposit_withdraw_cashout_routes_or_handlers():
    paths = {route.path for route in app.routes}
    for fragment in ["wallet", "deposit", "withdraw", "cashout"]:
        assert all(fragment not in path for path in paths)

    python_source = "\n".join(path.read_text(encoding="utf-8") for path in Path("app").rglob("*.py"))
    for term in ["deposit", "withdraw", "cashout", "wallet"]:
        assert f"def {term}" not in python_source
        assert f"api_{term}" not in python_source


def test_required_disclaimer_exactly_available():
    expected = """このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは購入・換金・出金・譲渡・外部ポイント交換・暗号資産交換・景品交換ができません。
このアプリはPolymarketへの注文、ウォレット接続、入金、出金、売買を行いません。"""
    assert DISCLAIMER == expected
