"""Disabled public market WebSocket skeleton.

This module is intentionally inert. A future version may listen to public market
updates, but this MVP uses REST polling and never starts a WebSocket connection
automatically.
"""


class DisabledMarketWebSocket:
    enabled = False

    def start(self) -> None:
        raise RuntimeError("Public market WebSocket support is disabled in this MVP.")
