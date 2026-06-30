DISCLAIMER = """このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは購入・換金・出金・譲渡・外部ポイント交換・暗号資産交換・景品交換ができません。
このアプリはPolymarketへの注文、ウォレット接続、入金、出金、売買を行いません。"""

FORBIDDEN_ROUTE_PATHS = {
    "/buy",
    "/sell",
    "/bet",
    "/deposit",
    "/withdraw",
    "/cashout",
    "/wallet",
    "/order/place",
    "/api/demo/bet",
    "/api/order",
    "/api/trade",
    "/api/wallet",
}


def assert_safe_route_path(path: str) -> None:
    if path in FORBIDDEN_ROUTE_PATHS:
        raise ValueError(f"Forbidden route path: {path}")
