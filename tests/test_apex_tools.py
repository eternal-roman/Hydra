"""Apex/companion tools — v1.1 additions: journal access, chart tools, allowlist.

These tests are read-only and use a FakeAgent that exposes the shape
of hydra_agent.HydraAgent expected by tools_readonly: a .broadcaster
with .latest_state and an .engines dict of engine-like objects with
.candles attributes.
"""
from __future__ import annotations
import os
import sys
import json
import pathlib
from types import SimpleNamespace
from dataclasses import dataclass

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from hydra_companions import tools_readonly as t
from hydra_engine import Candle


# ───── Fakes ─────

class FakeBroadcaster:
    def __init__(self, state: dict):
        self.latest_state = state


class FakeEngine:
    def __init__(self, candles):
        self.candles = list(candles)


class FakeAgent:
    def __init__(self, state: dict, engines: dict):
        self.broadcaster = FakeBroadcaster(state)
        self.engines = engines


def _make_candles(n=60, start=100.0, step=0.5):
    out = []
    price = start
    for i in range(n):
        o = price
        h = price + 1.2
        l = price - 1.1
        c = price + (0.3 if i % 3 else -0.4)
        out.append(Candle(open=o, high=h, low=l, close=c, volume=1000 + i * 10, timestamp=1_700_000_000 + i * 900))
        price = c + step * (1 if i % 2 else -1)
    return out


def _sample_state():
    # mirrors broadcast snapshot shape; values are plausible enough for tools
    return {
        "tick": 42,
        "mode": "competition",
        "pairs": {
            "BTC/USDC": {
                "regime": "RANGING",
                "strategy": "MEAN_REVERSION",
                "price": 76600.0,
                "indicators": {
                    "rsi": 25.8,
                    "atr_pct": 0.9,
                    "bb_lower": 76500.0,
                    "bb_middle": 76800.0,
                    "bb_upper": 77100.0,
                },
                "last_signal": {"action": "BUY", "confidence": 0.85, "reason": "mr buy"},
                "portfolio": {"position": 0.001, "avg_entry": 76600.0, "unrealized_pnl_pct": 0.0, "equity": 1000.0},
            },
            "SOL/USDC": {
                "regime": "TREND_UP",
                "strategy": "MOMENTUM",
                "price": 85.0,
                "indicators": {"rsi": 62.0, "atr_pct": 1.4, "bb_lower": 82.0, "bb_middle": 84.0, "bb_upper": 86.0},
                "last_signal": {"action": "HOLD", "confidence": 0.55},
                "portfolio": {"position": 0.0, "equity": 1000.0},
            },
        },
        "balance_usd": {"total_usd": 932.53},
        "order_journal": [
            {"placed_at": "2026-04-18T08:23:43.231064+00:00", "pair": "BTC/USDC", "side": "BUY",
             "intent": {"amount": 0.00098908, "limit_price": 76663.86},
             "lifecycle": {"state": "FILLED", "vol_exec": 0.00098908, "avg_fill_price": 76663.86, "fee_quote": 0.18957},
             "decision": {"strategy": "MEAN_REVERSION", "regime": "RANGING", "confidence": 0.8476, "reason": "mr buy"}},
            {"placed_at": "2026-04-18T08:43:43.330623+00:00", "pair": "BTC/USDC", "side": "BUY",
             "intent": {"amount": 0.00039521, "limit_price": 76546.05},
             "lifecycle": {"state": "FILLED", "vol_exec": 0.00039521, "avg_fill_price": 76546.05, "fee_quote": 0.07563},
             "decision": {"strategy": "MEAN_REVERSION", "regime": "RANGING", "confidence": 0.8515, "reason": "mr buy"}},
            {"placed_at": "2026-04-18T08:19:06.202146+00:00", "pair": "BTC/USDC", "side": "BUY",
             "intent": {"amount": 0.00090937, "limit_price": 76775.09},
             "lifecycle": {"state": "CANCELLED_UNFILLED", "vol_exec": 0.0, "avg_fill_price": None, "fee_quote": 0.0,
                            "terminal_reason": "Post only order"},
             "decision": {"strategy": "MEAN_REVERSION", "regime": "RANGING", "confidence": 0.8198, "reason": "ai adjust buy"}},
        ],
    }


def _agent():
    return FakeAgent(_sample_state(), {
        "BTC/USDC": FakeEngine(_make_candles(80, start=76000.0, step=40.0)),
        "SOL/USDC": FakeEngine(_make_candles(80, start=85.0, step=0.3)),
    })


# ───── Allowlist ─────

def test_check_tool_access_deny_by_default():
    assert t.check_tool_access({"id": "nobody"}, "get_chart_snapshot") is False


def test_check_tool_access_explicit_grant():
    soul = {"id": "apex", "capabilities": {"tool_access": ["get_chart_snapshot", "get_order_journal"]}}
    assert t.check_tool_access(soul, "get_chart_snapshot") is True
    assert t.check_tool_access(soul, "get_order_journal") is True
    assert t.check_tool_access(soul, "get_candles") is False


def test_enforce_tool_access_raises_on_deny():
    try:
        t.enforce_tool_access({"id": "apex", "capabilities": {"tool_access": []}}, "get_order_journal")
        raise AssertionError("should have raised")
    except t.ToolAccessDenied as e:
        assert "apex" in str(e)
        assert "get_order_journal" in str(e)


def test_enforce_tool_access_no_raise_on_allow():
    t.enforce_tool_access({"id": "apex", "capabilities": {"tool_access": ["get_order_journal"]}}, "get_order_journal")


def test_all_souls_have_expected_v11_tools():
    """Verify the three souls on disk all grant the v1.1 tool set."""
    souls_dir = ROOT / "hydra_companions" / "souls"
    expected = {"get_order_journal", "get_chart_snapshot", "get_chart_summary"}
    for sp in souls_dir.glob("*.soul.json"):
        raw = json.loads(sp.read_text(encoding="utf-8"))
        allow = set((raw.get("capabilities") or {}).get("tool_access") or [])
        assert expected.issubset(allow), f"{sp.stem}: missing {expected - allow}"


# ───── get_order_journal ─────

def test_get_order_journal_basic():
    a = _agent()
    out = t.get_order_journal(a, pair="BTC/USDC")
    assert out["count"] == 3
    # Sort ascending by placed_at (critical to the chronological protocol)
    times = [r["placed_at"] for r in out["trades"]]
    assert times == sorted(times), "journal must be returned chronologically ascending"


def test_get_order_journal_filter_by_state():
    a = _agent()
    out = t.get_order_journal(a, pair="BTC/USDC", state="FILLED")
    assert out["count"] == 2
    assert all(r["state"] == "FILLED" for r in out["trades"])


def test_get_order_journal_filter_by_strategy():
    a = _agent()
    out = t.get_order_journal(a, strategy="MEAN_REVERSION")
    assert all(r["strategy"] == "MEAN_REVERSION" for r in out["trades"])


def test_get_order_journal_limit_caps_and_flags_truncated():
    a = _agent()
    out = t.get_order_journal(a, limit=2)
    assert out["count"] == 2
    assert out["truncated"] is True


def test_get_order_journal_limit_hard_cap():
    a = _agent()
    out = t.get_order_journal(a, limit=100_000)
    assert out["filters"]["limit"] == 200


def test_get_order_journal_since_iso_filter():
    a = _agent()
    # All three entries are at 08:19, 08:23, 08:43 on 2026-04-18 — filtering
    # to entries at-or-after 08:30 should yield exactly one (the 08:43 fill).
    out = t.get_order_journal(a, pair="BTC/USDC", since_iso="2026-04-18T08:30:00+00:00")
    assert out["count"] == 1
    assert "08:43" in out["trades"][0]["placed_at"]


def test_get_order_journal_disk_fallback_when_memory_empty():
    """With empty memory journal, tool falls through to disk read."""
    a = FakeAgent({"tick": 0, "pairs": {}, "order_journal": []}, {})
    out = t.get_order_journal(a, limit=1)
    # Disk may be empty in a fresh checkout; just assert the source flag.
    assert out["source"] == "disk"


# ───── get_chart_snapshot ─────

def test_get_chart_snapshot_basic_shape():
    a = _agent()
    out = t.get_chart_snapshot(a, "BTC/USDC")
    assert out["pair"] == "BTC/USDC"
    assert out["regime"] == "RANGING"
    assert out["strategy"] == "MEAN_REVERSION"
    assert "structure" in out
    assert "bb_position_0_to_1" in out["structure"]
    assert "indicators" in out


def test_get_chart_snapshot_unknown_pair_returns_error():
    a = _agent()
    out = t.get_chart_snapshot(a, "DOGE/USDC")
    assert "error" in out
    assert "BTC/USDC" in out["available"]


def test_get_chart_snapshot_does_not_return_raw_candles():
    """Chart snapshot must remain token-tight — no raw OHLCV."""
    a = _agent()
    out = t.get_chart_snapshot(a, "BTC/USDC")
    for forbidden in ("candles", "ohlc", "ohlcv", "open", "high_low_series"):
        assert forbidden not in out, f"snapshot leaked {forbidden} field"


# ───── get_chart_summary ─────

def test_get_chart_summary_basic_shape():
    a = _agent()
    out = t.get_chart_summary(a, "BTC/USDC", lookback_n=50)
    assert out["pair"] == "BTC/USDC"
    assert out["lookback_candles"] >= 10
    assert "swing" in out
    assert "rsi" in out
    assert "atr_pct" in out
    assert "bb_touches_in_window" in out
    assert "directional_bias" in out


def test_get_chart_summary_does_not_return_raw_candles():
    a = _agent()
    out = t.get_chart_summary(a, "BTC/USDC", lookback_n=50)
    for forbidden in ("candles", "ohlc", "ohlcv", "raw_candles"):
        assert forbidden not in out


def test_get_chart_summary_clamps_lookback():
    a = _agent()
    out = t.get_chart_summary(a, "BTC/USDC", lookback_n=10_000)
    # Capped at 200 internally; cannot exceed available candle count either.
    assert out["lookback_candles"] <= 200


def test_get_chart_summary_no_engine_returns_error():
    a = FakeAgent(_sample_state(), {})  # no engines
    out = t.get_chart_summary(a, "BTC/USDC")
    assert "error" in out


# ───── compose_context_blob gating ─────

def test_compose_blob_chart_gated_on_allowlist():
    a = _agent()
    # Denied soul: chart section should not appear in blob
    denied = {"id": "stranger", "capabilities": {"tool_access": []}}
    blob = t.compose_context_blob(a, pair="BTC/USDC", soul=denied,
                                   include_chart=True, include_journal_tail=True)
    assert "[chart" not in blob
    assert "recent trades" not in blob

    # Granted soul: both sections present
    granted = {"id": "apex", "capabilities": {"tool_access": [
        "get_chart_snapshot", "get_order_journal"
    ]}}
    blob = t.compose_context_blob(a, pair="BTC/USDC", soul=granted,
                                   include_chart=True, include_journal_tail=True)
    assert "[chart BTC/USDC]" in blob
    assert "recent trades" in blob


def test_compose_blob_respects_max_bytes():
    a = _agent()
    granted = {"id": "apex", "capabilities": {"tool_access": [
        "get_chart_snapshot", "get_order_journal"
    ]}}
    blob = t.compose_context_blob(a, pair="BTC/USDC", max_bytes=200, soul=granted,
                                   include_chart=True, include_journal_tail=True)
    assert len(blob.encode("utf-8")) <= 200


# ───── TOOL_REGISTRY ─────

def test_tool_registry_has_v11_tools():
    assert "get_order_journal" in t.TOOL_REGISTRY
    assert "get_chart_snapshot" in t.TOOL_REGISTRY
    assert "get_chart_summary" in t.TOOL_REGISTRY


if __name__ == "__main__":
    for name, fn in list(globals().items()):
        if name.startswith("test_") and callable(fn):
            fn()
            print(f"  \u2713 {name}")
    print("all apex-tools tests passed")
