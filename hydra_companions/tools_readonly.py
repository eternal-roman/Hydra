"""Read-only companion tools.

Phase 1 ships with no tool use (companions chat only). These tool
implementations exist as plain Python functions that the companion
can call via an internal "mini-tool" pattern: the agent injects a
compact live-state blob into the first user message when the intent
suggests the companion needs market context. This avoids a tool-use
loop in Phase 1 while still letting companions reference real data.

Phase 2+ will expose these through the Anthropic tool-use API.
"""
from __future__ import annotations
from typing import Any


def _safe_snapshot(agent) -> dict:
    """Return a small dict of current live state. Never raises."""
    try:
        state = agent.broadcaster.latest_state or {}
    except Exception:
        return {}
    return state if isinstance(state, dict) else {}


def get_live_state(agent) -> dict:
    snap = _safe_snapshot(agent)
    # Trim to the fields companions actually reference.
    pairs = snap.get("pairs", {})
    trimmed_pairs = {}
    for pair, pdata in pairs.items():
        if not isinstance(pdata, dict):
            continue
        trimmed_pairs[pair] = {
            "regime": pdata.get("regime"),
            "strategy": pdata.get("strategy"),
            "signal": (pdata.get("last_signal") or {}).get("action"),
            "confidence": (pdata.get("last_signal") or {}).get("confidence"),
            "rsi": (pdata.get("indicators") or {}).get("rsi"),
            "atr_pct": (pdata.get("indicators") or {}).get("atr_pct"),
            "price": pdata.get("price"),
        }
    return {
        "tick": snap.get("tick"),
        "mode": snap.get("mode"),
        "pairs": trimmed_pairs,
    }


def get_pair_metrics(agent, pair: str) -> dict:
    snap = _safe_snapshot(agent)
    pdata = (snap.get("pairs") or {}).get(pair, {})
    if not pdata:
        return {"error": f"pair {pair} not found", "available": list((snap.get("pairs") or {}).keys())}
    return {
        "pair": pair,
        "price": pdata.get("price"),
        "regime": pdata.get("regime"),
        "strategy": pdata.get("strategy"),
        "indicators": pdata.get("indicators"),
        "last_signal": pdata.get("last_signal"),
        "portfolio": pdata.get("portfolio"),
    }


def get_positions(agent) -> dict:
    snap = _safe_snapshot(agent)
    out = {}
    for pair, pdata in (snap.get("pairs") or {}).items():
        pf = (pdata or {}).get("portfolio") or {}
        pos = pf.get("position") or 0.0
        if pos:
            out[pair] = {
                "size": pos,
                "avg_entry": pf.get("avg_entry"),
                "unrealized_pnl_pct": pf.get("unrealized_pnl_pct"),
                "equity": pf.get("equity"),
            }
    return {"open_positions": out}


def get_balance(agent) -> dict:
    snap = _safe_snapshot(agent)
    bal = snap.get("balance_usd") or {}
    return {"balance_usd": bal}


def get_recent_trades(agent, n: int = 10) -> dict:
    snap = _safe_snapshot(agent)
    trades = (snap.get("order_journal") or [])[-n:]
    return {"trades": trades, "count": len(trades)}


def get_brain_outputs(agent, pair: str) -> dict:
    snap = _safe_snapshot(agent)
    brain = snap.get("ai_brain") or {}
    per_pair = brain.get("per_pair") or {}
    return per_pair.get(pair) or {"status": "no brain output for this pair"}


def compose_context_blob(agent, *, pair: str | None = None, max_bytes: int = 2048) -> str:
    """Compose a compact context blob for injection into the user message.

    Used in Phase 1 as a cheap alternative to the full tool-use loop.
    Returns a short markdown-ish summary that fits inside `max_bytes`.
    """
    parts = []
    snap = _safe_snapshot(agent)
    tick = snap.get("tick")
    mode = snap.get("mode")
    parts.append(f"[tick={tick} mode={mode}]")

    pairs = snap.get("pairs") or {}
    targets = [pair] if pair and pair in pairs else list(pairs.keys())[:3]
    for p in targets:
        pd = pairs.get(p) or {}
        ind = pd.get("indicators") or {}
        sig = pd.get("last_signal") or {}
        pf = pd.get("portfolio") or {}
        parts.append(
            f"{p}: regime={pd.get('regime')} strat={pd.get('strategy')} "
            f"price={pd.get('price')} rsi={ind.get('rsi')} atr%={ind.get('atr_pct')} "
            f"signal={sig.get('action')}@{sig.get('confidence')} "
            f"pos={pf.get('position')} equity={pf.get('equity')}"
        )

    bal = snap.get("balance_usd") or {}
    if bal.get("total_usd") is not None:
        parts.append(f"balance_usd=${bal.get('total_usd'):.2f}")

    blob = "\n".join(parts)
    if len(blob.encode("utf-8")) > max_bytes:
        blob = blob[:max_bytes - 10] + "\n...[trunc]"
    return blob


# Registry of read-only tools. Not consumed in Phase 1 (companions
# currently reach live state via compose_context_blob injection rather
# than the Anthropic tool-use API). Phase 7 wires this into the
# tool-use loop so companions can explicitly fetch precisely the data
# they need per turn. Kept here so the full surface stays in one place.
TOOL_REGISTRY: dict[str, Any] = {
    "get_live_state": get_live_state,
    "get_pair_metrics": get_pair_metrics,
    "get_positions": get_positions,
    "get_balance": get_balance,
    "get_recent_trades": get_recent_trades,
    "get_brain_outputs": get_brain_outputs,
}
