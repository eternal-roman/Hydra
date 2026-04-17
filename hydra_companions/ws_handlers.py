"""Mount companion.* WS routes on the agent's broadcaster.

Namespace isolation: all routes use `companion.*` types, zero overlap
with LIVE state, backtest, or brain broadcasts.
"""
from __future__ import annotations


def mount_companion_routes(broadcaster, coordinator) -> None:
    """Register companion.* handlers on the broadcaster."""

    def on_connect(payload):
        return coordinator.handle_connect(payload)

    def on_message(payload):
        coordinator.handle_message(payload)
        # No direct reply; results broadcast async.
        return None

    def on_switch(payload):
        return coordinator.handle_switch(payload)

    broadcaster.register_handler("companion.connect", on_connect)
    broadcaster.register_handler("companion.message", on_message)
    broadcaster.register_handler("companion.switch", on_switch)
