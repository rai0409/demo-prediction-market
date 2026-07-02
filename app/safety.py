DISCLAIMER = """このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは金銭・暗号資産・外部ポイント・景品などと交換できず、譲渡もできません。
このアプリはPolymarketへの注文、暗号資産ウォレット接続、資金移動、実取引を行いません。"""

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
