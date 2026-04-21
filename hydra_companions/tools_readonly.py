"""Read-only companion tools.

Phase 1 ships with no tool use (companions chat only). These tool
implementations exist as plain Python functions that the companion
can call via an internal "mini-tool" pattern: the agent injects a
compact live-state blob into the first user message when the intent
suggests the companion needs market context. This avoids a tool-use
loop in Phase 1 while still letting companions reference real data.

Phase 2+ will expose these through the Anthropic tool-use API.

v1.1 adds:
- get_order_journal: full journal access with filters (memory-first,
  disk-fallback to hydra_order_journal.json for older entries)
- get_chart_snapshot: ultra-tight structural fingerprint for a pair
  (no raw OHLCV — summary only, token-capped)
- get_chart_summary: richer timeframe metrics over a lookback window
  (still no raw OHLCV — structural aggregates only)
- enforce_tool_access / check_tool_access: per-soul allowlist enforcement
  reading capabilities.tool_access from the soul JSON. Enforced in code
  so future per-companion permission changes take effect without edits.
"""
from __future__ import annotations
import json
import os
import statistics
from typing import Any, Optional


REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JOURNAL_PATH = os.path.join(REPO_ROOT, "hydra_order_journal.json")


# ───────────────────────────────────────────────────────────────
# PER-SOUL TOOL ACCESS ALLOWLIST
# ───────────────────────────────────────────────────────────────

class ToolAccessDenied(PermissionError):
    """Raised when a soul attempts a tool not on its capabilities.tool_access list."""


def check_tool_access(soul: dict, tool_name: str) -> bool:
    """Return True if this soul is allowed to use tool_name.

    Reads soul.capabilities.tool_access; a missing capabilities block
    means the soul has no tool access at all (deny-by-default). This
    is deliberate: a soul without an explicit capabilities declaration
    is not silently granted everything.
    """
    caps = (soul or {}).get("capabilities") or {}
    allowlist = caps.get("tool_access") or []
    return tool_name in allowlist


def enforce_tool_access(soul: dict, tool_name: str) -> None:
    """Raise ToolAccessDenied if this soul can't use tool_name.

    Intended for Phase-2 tool-use dispatch, and used defensively by
    compose_context_blob to avoid injecting data a soul can't see.
    """
    if not check_tool_access(soul, tool_name):
        soul_id = (soul or {}).get("id", "<unknown>")
        raise ToolAccessDenied(
            f"soul '{soul_id}' has no access to tool '{tool_name}' "
            f"(check capabilities.tool_access)"
        )


# ───────────────────────────────────────────────────────────────
# LIVE STATE TOOLS (unchanged — v1.0 behaviour preserved)
# ───────────────────────────────────────────────────────────────

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


# ───────────────────────────────────────────────────────────────
# NEW IN v1.1 — JOURNAL + CHART TOOLS
# ───────────────────────────────────────────────────────────────

_JOURNAL_TRIM_FIELDS = (
    "placed_at", "pair", "side",
)

_MAX_JOURNAL_LIMIT = 200


def _trim_journal_entry(e: dict) -> dict:
    """Return a compact entry — decision + intent + lifecycle summary only."""
    intent = e.get("intent") or {}
    lifecycle = e.get("lifecycle") or {}
    decision = e.get("decision") or {}
    return {
        "placed_at": e.get("placed_at"),
        "pair": e.get("pair"),
        "side": e.get("side"),
        "amount": intent.get("amount"),
        "limit_price": intent.get("limit_price"),
        "state": lifecycle.get("state"),
        "vol_exec": lifecycle.get("vol_exec"),
        "avg_fill_price": lifecycle.get("avg_fill_price"),
        "fee_quote": lifecycle.get("fee_quote"),
        "terminal_reason": lifecycle.get("terminal_reason"),
        "strategy": decision.get("strategy"),
        "regime": decision.get("regime"),
        "confidence": decision.get("confidence"),
        "reason": decision.get("reason"),
    }


def _load_journal_from_disk() -> list:
    """Read the full journal file from disk. Returns [] on any failure."""
    if not os.path.exists(JOURNAL_PATH):
        return []
    try:
        with open(JOURNAL_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, list) else []
    except (OSError, json.JSONDecodeError, ValueError):
        return []


def get_order_journal(agent, *,
                      pair: Optional[str] = None,
                      side: Optional[str] = None,
                      strategy: Optional[str] = None,
                      state: Optional[str] = None,
                      since_iso: Optional[str] = None,
                      limit: int = 50) -> dict:
    """Query the order journal with filters.

    Strategy: memory-first (broadcast snapshot's order_journal tail),
    disk-fallback to hydra_order_journal.json when since_iso predates
    the memory tail or when the memory tail is empty.

    Sort is ALWAYS ascending by placed_at (chronological). Callers
    that want most-recent-first must reverse the returned list
    themselves. This is deliberate — it matches Apex's
    chronological-before-indictment protocol.
    """
    if limit <= 0:
        limit = 1
    limit = min(int(limit), _MAX_JOURNAL_LIMIT)

    snap = _safe_snapshot(agent)
    mem_journal = snap.get("order_journal") or []

    use_disk = False
    if since_iso and mem_journal:
        first_mem = mem_journal[0].get("placed_at") if isinstance(mem_journal[0], dict) else None
        if first_mem and first_mem > since_iso:
            use_disk = True
    if not mem_journal:
        use_disk = True

    source = mem_journal if not use_disk else _load_journal_from_disk()

    def _match(e: dict) -> bool:
        if not isinstance(e, dict):
            return False
        if pair and e.get("pair") != pair:
            return False
        if side and e.get("side") != side:
            return False
        if since_iso:
            placed = e.get("placed_at") or ""
            if placed < since_iso:
                return False
        dec = e.get("decision") or {}
        life = e.get("lifecycle") or {}
        if strategy and dec.get("strategy") != strategy:
            return False
        if state and life.get("state") != state:
            return False
        return True

    filtered = [e for e in source if _match(e)]
    filtered.sort(key=lambda x: x.get("placed_at") or "")
    truncated = len(filtered) > limit
    filtered = filtered[-limit:] if truncated else filtered
    out = [_trim_journal_entry(e) for e in filtered]

    return {
        "trades": out,
        "count": len(out),
        "truncated": truncated,
        "source": "disk" if use_disk else "memory",
        "filters": {
            "pair": pair, "side": side, "strategy": strategy,
            "state": state, "since_iso": since_iso, "limit": limit,
        },
    }


def _get_engine(agent, pair: str):
    """Resolve an engine for a pair. Returns None if unavailable."""
    engines = getattr(agent, "engines", None)
    if not isinstance(engines, dict):
        return None
    return engines.get(pair)


def get_chart_snapshot(agent, pair: str) -> dict:
    """Ultra-tight structural fingerprint for a pair. Token-capped.

    Returns only what a trader needs to orient in one glance:
    regime, strategy, last signal, RSI / ATR% / BB position,
    last bb-touch recency, atr-expansion flag. No raw candles.
    """
    snap = _safe_snapshot(agent)
    pdata = (snap.get("pairs") or {}).get(pair) or {}
    if not pdata:
        return {
            "error": f"pair {pair} not found",
            "available": list((snap.get("pairs") or {}).keys()),
        }
    ind = pdata.get("indicators") or {}
    last_sig = pdata.get("last_signal") or {}
    price = pdata.get("price")

    bb_lower = ind.get("bb_lower")
    bb_middle = ind.get("bb_middle")
    bb_upper = ind.get("bb_upper")
    bb_position = None
    if price is not None and bb_lower is not None and bb_upper is not None and bb_upper > bb_lower:
        bb_position = round((price - bb_lower) / (bb_upper - bb_lower), 3)

    # Compute ATR expansion and recency flags from the engine if present.
    atr_expansion_ratio = None
    last_bb_lower_touch_ago = None
    last_bb_upper_touch_ago = None
    engine = _get_engine(agent, pair)
    if engine is not None:
        atr_pct_current = ind.get("atr_pct")
        try:
            series = getattr(engine, "_atr_pct_series_cache", None)
            if not series:
                from hydra_engine import Indicators
                series = Indicators.atr_pct_series(engine.candles)
            if series and atr_pct_current:
                non_zero = [x for x in series if x > 0]
                if non_zero:
                    median = statistics.median(non_zero)
                    if median > 0:
                        atr_expansion_ratio = round(atr_pct_current / median, 2)
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        try:
            candles = list(engine.candles)[-100:]
            for i, c in enumerate(reversed(candles)):
                if bb_lower is not None and c.low <= bb_lower and last_bb_lower_touch_ago is None:
                    last_bb_lower_touch_ago = i
                if bb_upper is not None and c.high >= bb_upper and last_bb_upper_touch_ago is None:
                    last_bb_upper_touch_ago = i
                if last_bb_lower_touch_ago is not None and last_bb_upper_touch_ago is not None:
                    break
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    return {
        "pair": pair,
        "price": price,
        "regime": pdata.get("regime"),
        "strategy": pdata.get("strategy"),
        "last_signal": {
            "action": last_sig.get("action"),
            "confidence": last_sig.get("confidence"),
            "reason": last_sig.get("reason"),
        },
        "indicators": {
            "rsi": ind.get("rsi"),
            "atr_pct": ind.get("atr_pct"),
            "bb_lower": bb_lower,
            "bb_middle": bb_middle,
            "bb_upper": bb_upper,
        },
        "structure": {
            "bb_position_0_to_1": bb_position,
            "atr_expansion_ratio_vs_median": atr_expansion_ratio,
            "last_bb_lower_touch_candles_ago": last_bb_lower_touch_ago,
            "last_bb_upper_touch_candles_ago": last_bb_upper_touch_ago,
        },
    }


def get_chart_summary(agent, pair: str, lookback_n: int = 50) -> dict:
    """Richer timeframe metrics over a lookback window. Still no raw OHLCV.

    Returns structural aggregates suitable for trade-setup reasoning:
    swing high/low, RSI range + current, ATR% median + current,
    BB touch counts, realized range, directional bias (EMA slope),
    count of distinct regime labels observed if the engine tracks them.
    """
    lookback_n = max(10, min(int(lookback_n or 50), 200))

    engine = _get_engine(agent, pair)
    if engine is None:
        return {"error": f"no engine for pair {pair}"}

    try:
        candles = list(engine.candles)[-lookback_n:]
        if len(candles) < 10:
            return {"error": f"insufficient candles for {pair} ({len(candles)} < 10)"}
    except Exception:
        return {"error": f"failed to read candles for {pair}"}

    highs = [c.high for c in candles]
    lows = [c.low for c in candles]
    closes = [c.close for c in candles]
    opens = [c.open for c in candles]
    volumes = [c.volume for c in candles]

    swing_high = max(highs)
    swing_low = min(lows)
    close_now = closes[-1]
    realized_range_pct = round((swing_high - swing_low) / swing_low * 100, 3) if swing_low > 0 else None

    try:
        from hydra_engine import Indicators
        rsi_series = [Indicators.rsi(closes[: i + 1]) for i in range(max(0, len(closes) - 20), len(closes))]
        rsi_series = [r for r in rsi_series if r is not None]
        rsi_current = rsi_series[-1] if rsi_series else None
        rsi_min = min(rsi_series) if rsi_series else None
        rsi_max = max(rsi_series) if rsi_series else None

        atr_pct_series = Indicators.atr_pct_series(candles)
        atr_pct_series = [x for x in atr_pct_series if x > 0]
        atr_pct_current = atr_pct_series[-1] if atr_pct_series else None
        atr_pct_median = round(statistics.median(atr_pct_series), 4) if atr_pct_series else None

        bb = Indicators.bollinger(closes)
        bb_lower = bb.get("lower")
        bb_upper = bb.get("upper")
    except Exception:
        rsi_current = rsi_min = rsi_max = None
        atr_pct_current = atr_pct_median = None
        bb_lower = bb_upper = None

    bb_lower_touches = sum(1 for c in candles if bb_lower is not None and c.low <= bb_lower)
    bb_upper_touches = sum(1 for c in candles if bb_upper is not None and c.high >= bb_upper)

    # Directional bias: simple linear fit of closes over the window,
    # normalized to percent change per candle.
    bias = None
    if len(closes) >= 5:
        n = len(closes)
        mean_x = (n - 1) / 2.0
        mean_y = sum(closes) / n
        num = sum((i - mean_x) * (closes[i] - mean_y) for i in range(n))
        den = sum((i - mean_x) ** 2 for i in range(n))
        if den > 0:
            slope = num / den
            bias_pct_per_candle = round(slope / mean_y * 100, 4) if mean_y > 0 else None
            bias = {
                "slope_pct_per_candle": bias_pct_per_candle,
                "direction": "up" if bias_pct_per_candle and bias_pct_per_candle > 0.01 else ("down" if bias_pct_per_candle and bias_pct_per_candle < -0.01 else "flat"),
            }

    return {
        "pair": pair,
        "lookback_candles": len(candles),
        "swing": {
            "swing_high": swing_high,
            "swing_low": swing_low,
            "realized_range_pct": realized_range_pct,
            "close_now": close_now,
            "position_in_range_0_to_1": round((close_now - swing_low) / (swing_high - swing_low), 3) if swing_high > swing_low else None,
        },
        "rsi": {"current": rsi_current, "min": rsi_min, "max": rsi_max},
        "atr_pct": {"current": atr_pct_current, "median_over_full_history": atr_pct_median},
        "bb_touches_in_window": {"lower": bb_lower_touches, "upper": bb_upper_touches},
        "directional_bias": bias,
        "volume_summary": {
            "mean": round(sum(volumes) / len(volumes), 2) if volumes else None,
            "max": max(volumes) if volumes else None,
        },
    }


# ───────────────────────────────────────────────────────────────
# CONTEXT BLOB COMPOSER (Phase-1 injection path)
# ───────────────────────────────────────────────────────────────

def compose_context_blob(agent, *, pair: Optional[str] = None,
                         max_bytes: int = 2048,
                         soul: Optional[dict] = None,
                         include_chart: bool = False,
                         include_journal_tail: bool = False) -> str:
    """Compose a compact context blob for injection into the user message.

    Used in Phase 1 as a cheap alternative to the full tool-use loop.
    Returns a short markdown-ish summary that fits inside `max_bytes`.

    New in v1.1: optional chart snapshot and journal tail inclusion,
    gated on the soul's capabilities.tool_access allowlist. If a soul
    isn't granted access to a tool, its data is silently omitted from
    the blob — no injection of denied data.
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

    # Chart snapshot — gated on allowlist via enforce_tool_access.
    # ToolAccessDenied is the genuine enforcement path; other exceptions
    # are swallowed so a transient tool fault doesn't break the blob.
    if include_chart and pair and soul is not None:
        try:
            enforce_tool_access(soul, "get_chart_snapshot")
            cs = get_chart_snapshot(agent, pair)
            if "error" not in cs:
                st = cs.get("structure") or {}
                parts.append(
                    f"[chart {pair}] bb_pos={st.get('bb_position_0_to_1')} "
                    f"atr_expansion={st.get('atr_expansion_ratio_vs_median')}× "
                    f"bb_lower_touch_ago={st.get('last_bb_lower_touch_candles_ago')} "
                    f"bb_upper_touch_ago={st.get('last_bb_upper_touch_candles_ago')}"
                )
        except ToolAccessDenied as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    # Journal tail — gated on allowlist via enforce_tool_access. When
    # access is granted but the journal is empty, emit an explicit
    # zero-entries line so the companion cannot hallucinate "journal
    # empty" from mere absence of a section (prior bug: Apex confidently
    # reported an empty journal when journal wasn't injected at all).
    if include_journal_tail and soul is not None:
        try:
            enforce_tool_access(soul, "get_order_journal")
            jr = get_order_journal(agent, pair=pair, limit=5)
            trades = jr.get("trades") or []
            src = jr.get("source") or "memory"
            if trades:
                parts.append(f"[recent trades (asc chronological, n={len(trades)}, source={src})]")
                for t in trades:
                    parts.append(
                        f"  {t.get('placed_at')} {t.get('side')} {t.get('amount')} @ "
                        f"{t.get('limit_price')} state={t.get('state')} conf={t.get('confidence')}"
                    )
            else:
                parts.append(f"[journal: 0 entries (source={src})]")
        except ToolAccessDenied as e:
            import logging; logging.warning(f"Ignored exception: {e}")
        except Exception as e:
            import logging; logging.warning(f"Ignored exception: {e}")

    blob = "\n".join(parts)
    suffix = "\n...[trunc]"
    if len(blob.encode("utf-8")) > max_bytes:
        suffix_bytes = len(suffix.encode("utf-8"))
        budget = max(0, max_bytes - suffix_bytes)
        # Iteratively trim until the encoded body fits under the budget —
        # safe for multi-byte characters (don't split mid-codepoint).
        while len(blob.encode("utf-8")) > budget and blob:
            blob = blob[:-1]
        blob = blob + suffix
    return blob


# Registry of read-only tools. In Phase 1 companions currently reach
# live state via compose_context_blob injection rather than the
# Anthropic tool-use API. Phase 7 wires this into the tool-use loop so
# companions can explicitly fetch precisely the data they need per
# turn. Kept here so the full surface stays in one place. Per-soul
# enforcement is via check_tool_access / enforce_tool_access above,
# reading the capabilities.tool_access list in each soul.json.
TOOL_REGISTRY: dict[str, Any] = {
    "get_live_state": get_live_state,
    "get_pair_metrics": get_pair_metrics,
    "get_positions": get_positions,
    "get_balance": get_balance,
    "get_recent_trades": get_recent_trades,
    "get_brain_outputs": get_brain_outputs,
    "get_order_journal": get_order_journal,
    "get_chart_snapshot": get_chart_snapshot,
    "get_chart_summary": get_chart_summary,
}
