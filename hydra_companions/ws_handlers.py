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

    # Phase 2+: proposals
    broadcaster.register_handler("companion.propose.trade", lambda p: coordinator.handle_propose_trade(p))
    broadcaster.register_handler("companion.propose.ladder", lambda p: coordinator.handle_propose_ladder(p))
    broadcaster.register_handler("companion.trade.confirm", lambda p: coordinator.handle_confirm(p))
    broadcaster.register_handler("companion.trade.reject", lambda p: coordinator.handle_reject(p))
    broadcaster.register_handler("companion.ladder.confirm", lambda p: coordinator.handle_confirm(p))
    broadcaster.register_handler("companion.ladder.reject", lambda p: coordinator.handle_reject(p))

    # Phase 5: memory
    broadcaster.register_handler("companion.memory.remember", lambda p: coordinator.handle_remember(p))
    broadcaster.register_handler("companion.memory.recall", lambda p: coordinator.handle_recall(p))
    broadcaster.register_handler("companion.memory.forget", lambda p: coordinator.handle_forget(p))
