DISCLAIMER = """このアプリはデモ用の予想マーケットビューアです。
表示される市場データは参考情報であり、投資・賭博・取引の推奨ではありません。
アプリ内のデモポイントは無償のシミュレーション専用ポイントです。
デモポイントは換金できず、譲渡もできません。
デモポイントは商品、ギフト券、Pay、株引換券、暗号資産とは交換できません。
このアプリはPolymarket公式・提携・公認サービスではなく、Polymarketへの注文、暗号資産ウォレット接続、資金移動、実取引を行いません。"""

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
