"""Read-only tool tests \u2014 contract + graceful degradation."""
import sys
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions import tools_readonly


class FakeBroadcaster:
    def __init__(self, state):
        self.latest_state = state


class FakeAgent:
    def __init__(self, state):
        self.broadcaster = FakeBroadcaster(state)


SAMPLE = {
    "tick": 42,
    "mode": "conservative",
    "pairs": {
        "SOL/USDC": {
            "price": 142.33,
            "regime": "TREND_UP",
            "strategy": "MOMENTUM",
            "indicators": {"rsi": 58.1, "atr_pct": 2.1},
            "last_signal": {"action": "HOLD", "confidence": 0.41},
            "portfolio": {"position": 0.5, "avg_entry": 140.1, "equity": 102.5},
        },
        "BTC/USDC": {
            "price": 58200,
            "regime": "RANGING",
            "strategy": "GRID",
            "indicators": {"rsi": 48.0, "atr_pct": 1.4},
            "last_signal": {"action": "HOLD", "confidence": 0.2},
            "portfolio": {"position": 0.0, "equity": 100.0},
        },
    },
    "balance_usd": {"total_usd": 202.5},
}


def test_get_live_state_trims_fields():
    agent = FakeAgent(SAMPLE)
    state = tools_readonly.get_live_state(agent)
    assert state["tick"] == 42
    assert "SOL/USDC" in state["pairs"]
    assert state["pairs"]["SOL/USDC"]["rsi"] == 58.1


def test_get_pair_metrics_unknown_pair():
    agent = FakeAgent(SAMPLE)
    result = tools_readonly.get_pair_metrics(agent, "DOGE/USDC")
    assert "error" in result
    assert "available" in result


def test_get_positions_only_open():
    agent = FakeAgent(SAMPLE)
    pos = tools_readonly.get_positions(agent)
    assert "SOL/USDC" in pos["open_positions"]
    assert "BTC/USDC" not in pos["open_positions"]


def test_get_balance():
    agent = FakeAgent(SAMPLE)
    bal = tools_readonly.get_balance(agent)
    assert bal["balance_usd"]["total_usd"] == 202.5


def test_compose_context_blob_fits_budget():
    agent = FakeAgent(SAMPLE)
    blob = tools_readonly.compose_context_blob(agent, max_bytes=500)
    assert isinstance(blob, str)
    assert len(blob.encode("utf-8")) <= 500


def test_tools_degrade_on_missing_state():
    agent = FakeAgent(None)
    assert tools_readonly.get_live_state(agent) == {"tick": None, "mode": None, "pairs": {}}
    assert tools_readonly.get_positions(agent) == {"open_positions": {}}


def test_tool_registry_no_write_tools():
    # Enforce that Phase 1 registry contains ONLY read tools.
    forbidden = ("place_order", "cancel_order", "propose_trade", "propose_ladder")
    for f in forbidden:
        assert f not in tools_readonly.TOOL_REGISTRY


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all tools_readonly tests passed")
