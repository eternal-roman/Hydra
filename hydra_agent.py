#!/usr/bin/env python3
"""
HYDRA Agent — Kraken CLI Integration Layer (Live Trading)

Connects the HYDRA engine to live Kraken market data via kraken-cli (WSL).
Supports live trading on SOL/USDC, SOL/BTC, and BTC/USDC.
Broadcasts state over WebSocket for the React dashboard.

Usage:
    python hydra_agent.py --pairs SOL/USDC,SOL/BTC --balance 100 --duration 600
    python hydra_agent.py --pairs SOL/USDC,SOL/BTC,BTC/USDC --interval 60
"""

import subprocess
import json
import time
import sys
import os
import argparse
import queue
import signal as sig
import asyncio
import threading
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any, Tuple

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

# Force UTF-8 on stdout/stderr so non-ASCII glyphs in status prints (e.g. ∞)
# don't crash the tick loop under Windows cmd.exe's default cp1252 codepage.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, OSError):
        pass

# Load .env file if present (no dependency needed)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                _v = _v.strip()
                # Strip surrounding quotes (single or double)
                if len(_v) >= 2 and _v[0] == _v[-1] and _v[0] in ('"', "'"):
                    _v = _v[1:-1]
                if _v and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v

from hydra_engine import HydraEngine, CrossPairCoordinator, OrderBookAnalyzer, PositionSizer, SIZING_CONSERVATIVE, SIZING_COMPETITION
from hydra_tuner import ParameterTracker
from hydra_thesis import ThesisTracker
from hydra_thesis_processor import ThesisProcessorWorker
from hydra_journal_migrator import migrate_legacy_trade_log_file

try:
    from hydra_brain import HydraBrain
    HAS_BRAIN = True
except ImportError:
    HAS_BRAIN = False

# ═══════════════════════════════════════════════════════════════
# KRAKEN CLI WRAPPER (via WSL)
# ═══════════════════════════════════════════════════════════════

class KrakenCLI:
    """Wraps kraken-cli v0.2.3 running in WSL Ubuntu."""

    # REST & WS pair resolution: friendly name → CLI format.
    # Internal canonical uses BTC (modern Kraken convention). The CLI
    # accepts slashed BTC form natively (SOL/BTC, BTC/USDC). Legacy XBT
    # aliases are kept so old snapshots/journals resolve correctly.
    PAIR_MAP = {
        "SOL/USDC": "SOL/USDC",
        "SOL/BTC": "SOL/BTC",
        "BTC/USDC": "BTC/USDC",
        "BTC/USD": "BTC/USD",
        # Legacy XBT aliases — resolve to BTC canonical
        "SOL/XBT": "SOL/BTC",
        "XBT/USDC": "BTC/USDC",
    }

    # WS v2 API pair resolution: friendly name → WS v2 format.
    # With BTC as canonical, WS v2 format matches internal names directly.
    WS_PAIR_MAP = {
        "SOL/USDC": "SOL/USDC",
        "SOL/BTC": "SOL/BTC",
        "BTC/USDC": "BTC/USDC",
    }

    # Suffixes Kraken uses for non-tradable (staked/bonded/locked) assets
    STAKED_SUFFIXES = ('.B', '.S', '.M')

    # Kraken sometimes returns extended asset names — normalize to canonical form
    ASSET_NORMALIZE = {
        'XXBT': 'BTC', 'XBTC': 'BTC', 'XBT': 'BTC',
        'XETH': 'ETH', 'XSOL': 'SOL',
        'ZUSD': 'USD', 'ZUSDC': 'USDC',
    }

    # Per-pair price precision (hardcoded fallbacks). Dynamically overridden
    # at startup by load_pair_constants() → apply_pair_constants() from
    # `kraken pairs`. Legacy XBT aliases kept so _format_price works with
    # old journal/snapshot data.
    PRICE_DECIMALS = {
        'SOL/USDC': 2, 'SOLUSDC': 2,
        'BTC/USDC': 1, 'BTCUSDC': 1,
        'SOL/BTC': 7, 'SOLBTC': 7,
        'BTC/USD': 1, 'BTCUSD': 1,
        # Legacy XBT aliases
        'XBT/USDC': 1, 'XBTUSDC': 1,
        'SOL/XBT': 7, 'SOLXBT': 7,
        'XBT/USD': 1, 'XBTUSD': 1,
    }
    PRICE_DECIMALS_DEFAULT = 8  # conservative fallback for unknown pairs

    @staticmethod
    def _is_staked(asset: str) -> bool:
        """Check if an asset name represents a staked/bonded/locked position."""
        return any(asset.endswith(s) for s in KrakenCLI.STAKED_SUFFIXES)

    @staticmethod
    def _normalize_asset(asset: str) -> str:
        """Normalize Kraken asset name to canonical form (e.g. XXBT → BTC).
        Strips staked suffixes first, then applies name mapping."""
        name = asset
        for suffix in KrakenCLI.STAKED_SUFFIXES:
            if name.endswith(suffix):
                name = name[:-len(suffix)]
                break
        return KrakenCLI.ASSET_NORMALIZE.get(name, name)

    @staticmethod
    def _run(args: list, timeout: int = 20) -> dict:
        """Execute a kraken CLI command via WSL and return parsed JSON."""
        cmd_str = f"source ~/.cargo/env && kraken {' '.join(args)} -o json 2>/dev/null"
        cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd_str]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            stdout = result.stdout.strip()
            if not stdout:
                return {"error": f"Empty response (exit code {result.returncode})"}
            data = json.loads(stdout)
            if isinstance(data, dict) and "error" in data:
                return data
            return data
        except subprocess.TimeoutExpired:
            return {"error": "Command timed out", "retryable": True}
        except json.JSONDecodeError as e:
            return {"error": f"JSON parse error: {e}", "raw": stdout[:200] if stdout else ""}
        except Exception as e:
            return {"error": str(e)}

    @staticmethod
    def _resolve_pair(pair: str) -> str:
        """Resolve to CLI pair format (e.g. SOL/BTC, BTC/USDC)."""
        return KrakenCLI.PAIR_MAP.get(pair, pair)

    @staticmethod
    def _resolve_ws_pair(pair: str) -> str:
        """Resolve to WS v2 pair format (e.g. SOL/BTC, BTC/USDC)."""
        return KrakenCLI.WS_PAIR_MAP.get(pair, pair)

    @staticmethod
    def _format_price(pair: str, price: float) -> str:
        """HF-001 fix: format a price at the pair's native precision.

        Looks up the pair in PRICE_DECIMALS (accepting the friendly form with
        slash, the slashless form, or the PAIR_MAP-resolved form). Falls back
        to PRICE_DECIMALS_DEFAULT (8) for unknown pairs. Rounds the price to
        the allowed number of decimals and formats with trailing zeros to 8dp
        (Kraken accepts trailing zeros as insignificant but rejects meaningful
        decimals beyond the pair's precision).
        """
        decimals = (
            KrakenCLI.PRICE_DECIMALS.get(pair)
            or KrakenCLI.PRICE_DECIMALS.get(pair.replace("/", ""))
            or KrakenCLI.PRICE_DECIMALS.get(KrakenCLI._resolve_pair(pair))
            or KrakenCLI.PRICE_DECIMALS_DEFAULT
        )
        rounded = round(float(price), decimals)
        return f"{rounded:.8f}"

    # ─── System Status ───

    @staticmethod
    def system_status() -> dict:
        """Get Kraken system status.

        Returns {"status": "online"|"cancel_only"|"post_only"|"maintenance",
                 "timestamp": "..."} or {"error": "..."} on failure.
        """
        return KrakenCLI._run(["status"])

    # ─── Asset Pair Info ───

    @staticmethod
    def asset_pairs(pairs: list = None) -> dict:
        """Get tradable asset pair info.

        Returns {pair_name: {pair_decimals, ordermin, costmin, base, quote, ...}}
        or {"error": "..."} on failure.
        """
        args = ["pairs"]
        if pairs:
            resolved = ",".join(KrakenCLI._resolve_pair(p) for p in pairs)
            args.extend(["--pair", resolved])
        return KrakenCLI._run(args)

    @classmethod
    def load_pair_constants(cls, pairs: list) -> dict:
        """Fetch pair info from Kraken and return normalized constants.

        Returns {friendly_pair: {price_decimals, ordermin, costmin, base, quote,
        lot_decimals, tick_size}} for each requested pair that Kraken knows about.
        Returns {} on API failure (caller should use hardcoded fallbacks).
        """
        data = cls.asset_pairs(pairs)
        if not isinstance(data, dict) or "error" in data:
            return {}

        # Build reverse map: every form Kraken might use → friendly pair name.
        # Includes legacy XBT aliases so Kraken's wsname/altname (which still
        # use XBT internally) resolve to our BTC canonical pairs.
        friendly_map = {}
        for fp in pairs:
            resolved = cls._resolve_pair(fp)
            friendly_map[fp] = fp
            friendly_map[fp.replace("/", "")] = fp
            friendly_map[resolved] = fp
            friendly_map[resolved.replace("/", "")] = fp
        # Add legacy XBT alias entries: Kraken pairs API returns wsname="XBT/USDC",
        # altname="XBTUSDC" etc. Map those back to our BTC canonical pairs.
        for alias, target in cls.PAIR_MAP.items():
            if alias != target:  # only aliases (XBT→BTC mappings)
                for fp in pairs:
                    if cls._resolve_pair(fp) == target:
                        friendly_map[alias] = fp
                        friendly_map[alias.replace("/", "")] = fp
                        break

        result = {}
        for kraken_name, info in data.items():
            if not isinstance(info, dict):
                continue
            friendly = (
                friendly_map.get(info.get("wsname"))
                or friendly_map.get(info.get("altname"))
                or friendly_map.get(kraken_name)
                or friendly_map.get(kraken_name.replace("/", ""))
            )
            if not friendly:
                continue
            base = cls._normalize_asset(info.get("base", ""))
            quote = cls._normalize_asset(info.get("quote", ""))
            result[friendly] = {
                "price_decimals": int(info.get("pair_decimals", cls.PRICE_DECIMALS_DEFAULT)),
                "ordermin": float(info.get("ordermin", 0.02)),
                "costmin": float(info.get("costmin", 0.5)),
                "base": base,
                "quote": quote,
                "lot_decimals": int(info.get("lot_decimals", 8)),
                "tick_size": info.get("tick_size"),
            }
        return result

    @classmethod
    def apply_pair_constants(cls, loaded: dict):
        """Merge dynamically loaded pair constants into class-level PRICE_DECIMALS."""
        for friendly, info in loaded.items():
            dec = info["price_decimals"]
            cls.PRICE_DECIMALS[friendly] = dec
            cls.PRICE_DECIMALS[friendly.replace("/", "")] = dec
            resolved = cls._resolve_pair(friendly)
            cls.PRICE_DECIMALS[resolved] = dec
            cls.PRICE_DECIMALS[resolved.replace("/", "")] = dec

    # ─── Public Market Data ───

    @staticmethod
    def ticker(pair: str) -> dict:
        """Get current ticker data."""
        p = KrakenCLI._resolve_pair(pair)
        data = KrakenCLI._run(["ticker", p])
        if "error" in data:
            return data
        for key, val in data.items():
            if isinstance(val, dict) and "c" in val:
                return {
                    "pair": pair,
                    "price": float(val["c"][0]) if val.get("c") else 0,
                    "ask": float(val["a"][0]) if val.get("a") else 0,
                    "bid": float(val["b"][0]) if val.get("b") else 0,
                    "high_24h": float(val["h"][1]) if len(val.get("h", [])) > 1 else 0,
                    "low_24h": float(val["l"][1]) if len(val.get("l", [])) > 1 else 0,
                    "volume_24h": float(val["v"][1]) if len(val.get("v", [])) > 1 else 0,
                    "open": float(val.get("o", 0)),
                }
        return data

    @staticmethod
    def ohlc(pair: str, interval: int = 1) -> list:
        """Fetch OHLC candles. Returns list of candle dicts."""
        p = KrakenCLI._resolve_pair(pair)
        data = KrakenCLI._run(["ohlc", p, "--interval", str(interval)])
        if isinstance(data, dict) and "error" in data:
            print(f"  [WARN] OHLC fetch error for {pair}: {data['error']}")
            return []
        candles = []
        if isinstance(data, dict):
            for key, values in data.items():
                if key in ("error", "last"):
                    continue
                if isinstance(values, list):
                    for row in values:
                        if isinstance(row, list) and len(row) >= 7:
                            candles.append({
                                "timestamp": float(row[0]),
                                "open": float(row[1]),
                                "high": float(row[2]),
                                "low": float(row[3]),
                                "close": float(row[4]),
                                "volume": float(row[6]),
                            })
        return candles

    # ─── Private Account ───

    @staticmethod
    def balance() -> dict:
        """Get account balance. Returns {asset: amount} for non-zero balances."""
        data = KrakenCLI._run(["balance"])
        if isinstance(data, dict) and "error" not in data:
            return {k: float(v) for k, v in data.items() if float(v) > 0}
        return data

    @staticmethod
    def trades_history(start: float = None, end: float = None) -> dict:
        """Get trade history, optionally filtered by time range.

        start/end: Unix timestamps. Returns {"count": N, "trades": {trade_id: {...}}}.
        """
        args = ["trades-history"]
        if start is not None:
            args.extend(["--start", str(start)])
        if end is not None:
            args.extend(["--end", str(end)])
        return KrakenCLI._run(args)

    @staticmethod
    def volume(pairs=None) -> dict:
        """Get 30-day trade volume and current fee tier.

        pairs: optional list of friendly pair symbols (e.g. ["SOL/USDC","BTC/USDC"])
        or a pre-formatted comma-separated string. Returns raw Kraken response dict,
        or {"error": ...} on failure.
        """
        args = ["volume"]
        if pairs:
            if isinstance(pairs, (list, tuple)):
                resolved = ",".join(KrakenCLI._resolve_pair(p) for p in pairs)
            else:
                resolved = pairs
            args.extend(["--pair", resolved])
        return KrakenCLI._run(args)

    # ─── Order Execution ───

    @staticmethod
    def order_buy(pair: str, volume: float, price: float = None,
                  order_type: str = "limit", post_only: bool = True,
                  validate: bool = False, userref: int = None) -> dict:
        """Place a buy order. Defaults to limit post-only (maker).

        `userref` is the numeric client tag that flows back to us via
        `order_userref` on the WS executions stream — our primary
        correlation key between a local journal entry and the exchange.
        """
        p = KrakenCLI._resolve_pair(pair)
        args = ["order", "buy", p, f"{volume:.8f}", "--type", order_type, "--yes"]
        if price is not None and order_type != "market":
            args.extend(["--price", KrakenCLI._format_price(pair, price)])
        if post_only and order_type == "limit":
            args.extend(["--oflags", "post"])
        if userref is not None:
            args.extend(["--userref", str(int(userref))])
        if validate:
            args.append("--validate")
        return KrakenCLI._run(args)

    @staticmethod
    def order_sell(pair: str, volume: float, price: float = None,
                   order_type: str = "limit", post_only: bool = True,
                   validate: bool = False, userref: int = None) -> dict:
        """Place a sell order. Defaults to limit post-only (maker).

        `userref` is the numeric client tag that flows back to us via
        `order_userref` on the WS executions stream — our primary
        correlation key between a local journal entry and the exchange.
        """
        p = KrakenCLI._resolve_pair(pair)
        args = ["order", "sell", p, f"{volume:.8f}", "--type", order_type, "--yes"]
        if price is not None and order_type != "market":
            args.extend(["--price", KrakenCLI._format_price(pair, price)])
        if post_only and order_type == "limit":
            args.extend(["--oflags", "post"])
        if userref is not None:
            args.extend(["--userref", str(int(userref))])
        if validate:
            args.append("--validate")
        return KrakenCLI._run(args)

    @staticmethod
    def query_orders(*txids, userref: int = None, trades: bool = False) -> dict:
        """Query specific orders by txid or userref.

        Returns {txid: {status, vol_exec, price, fee, ...}} for each order,
        or {"error": "..."} on failure.
        """
        args = ["query-orders"]
        if txids:
            args.extend([str(t) for t in txids])
        if userref is not None:
            args.extend(["--userref", str(userref)])
        if trades:
            args.append("--trades")
        return KrakenCLI._run(args)

    @staticmethod
    def cancel_order(*txids) -> dict:
        """Cancel specific order(s) by txid.

        Returns Kraken response (typically {"count": N}) or {"error": "..."}.
        """
        args = ["order", "cancel"]
        args.extend([str(t) for t in txids])
        args.append("--yes")
        return KrakenCLI._run(args)

    @staticmethod
    def cancel_after(seconds: int = 60) -> dict:
        """Dead man's switch — cancel all orders after timeout."""
        return KrakenCLI._run(["order", "cancel-after", str(seconds)])

    @staticmethod
    def cancel_all() -> dict:
        """Cancel all open orders."""
        return KrakenCLI._run(["order", "cancel-all", "--yes"])

    # ─── Paper Trading ───

    @staticmethod
    def paper_buy(pair: str, volume: float, order_type: str = "market") -> dict:
        """Paper trade buy — no API keys needed."""
        p = KrakenCLI._resolve_pair(pair)
        return KrakenCLI._run(["paper", "buy", p, "--type", order_type, "--volume", f"{volume:.8f}"])

    @staticmethod
    def paper_sell(pair: str, volume: float, order_type: str = "market") -> dict:
        """Paper trade sell — no API keys needed."""
        p = KrakenCLI._resolve_pair(pair)
        return KrakenCLI._run(["paper", "sell", p, "--type", order_type, "--volume", f"{volume:.8f}"])

    @staticmethod
    def paper_balance() -> dict:
        """Get paper trading balance."""
        return KrakenCLI._run(["paper", "balance"])



# ═══════════════════════════════════════════════════════════════
# WEBSOCKET BROADCAST SERVER (for React Dashboard)
# ═══════════════════════════════════════════════════════════════

class DashboardBroadcaster:
    """Async WebSocket server that broadcasts agent state to dashboard clients.

    Phase 6 refactor (v2.10.0): adds message-type discrimination for the
    backtest observer, experiment library, and review stream.

    Outbound:
      - `broadcast(state)` — live per-tick state. With `compat_mode=True`
        (default), sends BOTH the legacy raw state dict and the new
        wrapped `{"type": "state", "data": state}` form. Existing
        dashboards keep reading raw; the Phase 8 dashboard reads wrapped.
      - `broadcast_message(type, payload)` — new type-discriminated
        message (e.g., backtest_progress). Always wrapped; legacy
        dashboards ignore unknown shapes.

    Inbound (Phase 6 additive):
      - `register_handler(type, fn)` — route JSON messages matching
        `{"type": type, ...}` to `fn(payload) -> Optional[dict]`. The
        return dict is sent back as `{"type": f"{type}_ack", ...reply}`.
      - Unknown message types are silently ignored (we don't want the
        dashboard DoS'ing the agent via malformed messages).

    Threading: the asyncio loop runs in a daemon thread. `broadcast_*`
    is thread-safe (uses run_coroutine_threadsafe). Handlers execute
    on the asyncio loop thread, so long work should be handed off
    (handlers in this codebase return quickly — they queue into
    BacktestWorkerPool).
    """

    def __init__(self, host: str = "127.0.0.1", port: int = 8765,
                 compat_mode: bool = True):
        self.host = host
        self.port = port
        self.clients = set()
        self.latest_state = {}
        self._loop = None
        self._thread = None
        self._handlers: Dict[str, Any] = {}
        self.compat_mode = compat_mode

    def start(self):
        """Start WebSocket server in a background thread."""
        self._thread = threading.Thread(target=self._run_loop, daemon=True)
        self._thread.start()

    def _run_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop.run_until_complete(self._serve())

    async def _serve(self):
        try:
            import websockets
            async with websockets.serve(self._handler, self.host, self.port):
                print(f"  [WS] Dashboard server running on ws://{self.host}:{self.port}")
                await asyncio.Future()  # run forever
        except ImportError:
            print("  [WS] websockets package not installed — dashboard feed disabled")
            print("  [WS] Install with: pip install websockets")

    async def _handler(self, websocket):
        self.clients.add(websocket)
        print(f"  [WS] Dashboard client connected ({len(self.clients)} total)")
        try:
            # Send latest state immediately on connect (both formats if compat)
            if self.latest_state:
                if self.compat_mode:
                    await websocket.send(json.dumps(self.latest_state))
                await websocket.send(json.dumps({
                    "type": "state", "data": self.latest_state,
                }))
            async for raw in websocket:
                try:
                    await self._dispatch_inbound(raw, websocket)
                except Exception as e:
                    # Never let a malformed message break the connection.
                    print(f"  [WS] inbound dispatch error: {type(e).__name__}: {e}")
        except Exception as e:
            if not isinstance(e, (ConnectionError, OSError)):
                print(f"  [WS] Client handler error: {type(e).__name__}: {e}")
        finally:
            self.clients.discard(websocket)
            print(f"  [WS] Dashboard client disconnected ({len(self.clients)} total)")

    async def _dispatch_inbound(self, raw, websocket):
        try:
            msg = json.loads(raw)
        except (TypeError, ValueError):
            return  # silently ignore non-JSON
        if not isinstance(msg, dict):
            return
        msg_type = msg.get("type")
        handler = self._handlers.get(msg_type) if msg_type else None
        if handler is None:
            return
        payload = {k: v for k, v in msg.items() if k != "type"}
        try:
            reply = handler(payload)
        except Exception as e:
            reply = {"success": False, "error": f"{type(e).__name__}: {e}"}
        if reply is None:
            return
        try:
            ack_type = f"{msg_type}_ack"
            await websocket.send(json.dumps({"type": ack_type, **reply}))
        except Exception:
            # Client likely dropped mid-send — next broadcast will reap it
            pass

    def register_handler(self, msg_type: str, fn) -> None:
        """Route inbound messages with matching `type` to `fn(payload)`."""
        self._handlers[msg_type] = fn

    def broadcast(self, state: dict):
        """Broadcast live tick state to all connected dashboard clients.

        `compat_mode=True` emits BOTH the legacy raw state (what v2.9.x
        dashboards read) and the new wrapped `{type: "state", data}` form
        (what Phase 8 dashboards read). Set `compat_mode=False` after the
        dashboard refactor lands to halve per-tick WS bandwidth.
        """
        self.latest_state = state
        if not (self._loop and self.clients):
            return
        wrapped = json.dumps({"type": "state", "data": state})
        raw = json.dumps(state) if self.compat_mode else None
        for client in list(self.clients):
            if raw is not None:
                asyncio.run_coroutine_threadsafe(
                    self._safe_send(client, raw), self._loop
                )
            asyncio.run_coroutine_threadsafe(
                self._safe_send(client, wrapped), self._loop
            )

    def broadcast_message(self, msg_type: str, payload: dict):
        """Emit a typed message (never wrapped as `state`). Always uses
        the `{type, ...payload}` format.  Safe to call from any thread.
        """
        if not (self._loop and self.clients):
            return
        msg = json.dumps({"type": msg_type, **payload})
        for client in list(self.clients):
            asyncio.run_coroutine_threadsafe(
                self._safe_send(client, msg), self._loop
            )

    async def _safe_send(self, client, msg):
        try:
            await client.send(msg)
        except Exception:
            self.clients.discard(client)


# ═══════════════════════════════════════════════════════════════
# BASE STREAM — shared WS subprocess/reader/health infrastructure
# ═══════════════════════════════════════════════════════════════

class BaseStream:
    """Shared infrastructure for all Kraken WS CLI subprocess streams.

    Subclasses override:
        _build_cmd() -> str   — the bash command inside WSL
        _on_message(msg)      — handle one parsed JSON message
        _stream_label() -> str — short label for log lines (e.g. "EXECSTREAM")
    """

    HEARTBEAT_TIMEOUT_S = 30.0
    READER_JOIN_TIMEOUT_S = 5.0
    RESTART_COOLDOWN_S = 30.0

    def __init__(self, paper: bool = False):
        self.paper = paper
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._last_heartbeat: float = 0.0
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        self._reader_exit_reason: Optional[str] = None
        self._last_restart_attempt: float = 0.0
        self._restart_count: int = 0

    def _build_cmd(self) -> str:
        """Return the bash command to run inside WSL. Subclasses must override."""
        raise NotImplementedError

    def _on_message(self, msg: Dict[str, Any]) -> None:
        """Handle one parsed JSON message. Subclasses must override."""
        raise NotImplementedError

    def _stream_label(self) -> str:
        """Short label for log lines. Override for a better name."""
        return "STREAM"

    def _on_heartbeat(self) -> None:
        """Bump the heartbeat timestamp. Call from _on_message on any
        liveness-indicating traffic."""
        self._last_heartbeat = time.monotonic()

    # ───────── lifecycle ─────────

    def start(self) -> bool:
        """Spawn the subprocess and reader/stderr threads. Returns True on success."""
        if self.paper:
            self._last_heartbeat = time.monotonic()
            return True
        self._shutdown.clear()
        self._reader_exit_reason = None
        self._on_start_reset()
        label = self._stream_label()
        cmd = [
            "wsl", "-d", "Ubuntu", "--", "bash", "-c",
            f"source ~/.cargo/env && {self._build_cmd()}",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, text=True,
            )
        except Exception as e:
            print(f"  [{label}] failed to spawn subprocess: {type(e).__name__}: {e}")
            return False
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name=f"{label}-reader", daemon=True,
        )
        self._reader_thread.start()
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name=f"{label}-stderr", daemon=True,
        )
        self._stderr_thread.start()
        self._last_heartbeat = time.monotonic()
        print(f"  [{label}] stream started")
        return True

    def _on_start_reset(self) -> None:
        """Hook for subclasses to reset state on (re)start. Called before spawn."""
        pass

    def stop(self) -> None:
        """Terminate subprocess, join reader and stderr threads. Idempotent."""
        self._shutdown.set()
        if self._proc is not None:
            try:
                self._proc.terminate()
                self._proc.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    self._proc.kill()
                except Exception:
                    pass
            except Exception:
                pass
            self._proc = None
        for attr in ("_reader_thread", "_stderr_thread"):
            t = getattr(self, attr, None)
            if t is not None:
                try:
                    t.join(timeout=self.READER_JOIN_TIMEOUT_S)
                except Exception:
                    pass
                setattr(self, attr, None)

    @property
    def healthy(self) -> bool:
        return self.health_status()[0]

    def health_status(self) -> Tuple[bool, str]:
        if self.paper:
            return True, ""
        if self._proc is None:
            return False, "subprocess not started"
        rc = self._proc.poll()
        if rc is not None:
            return False, f"subprocess exited (rc={rc})"
        if self._reader_thread is None or not self._reader_thread.is_alive():
            reason = self._reader_exit_reason or "exited (reason unknown)"
            return False, f"reader thread {reason}"
        age = time.monotonic() - self._last_heartbeat
        if age > self.HEARTBEAT_TIMEOUT_S:
            return False, (
                f"no heartbeat for {age:.0f}s "
                f"(threshold {self.HEARTBEAT_TIMEOUT_S:.0f}s)"
            )
        return True, ""

    def ensure_healthy(self) -> Tuple[bool, str]:
        if self.paper:
            return True, ""
        healthy, reason = self.health_status()
        if healthy:
            return True, ""
        now = time.monotonic()
        if now - self._last_restart_attempt < self.RESTART_COOLDOWN_S:
            return healthy, reason
        self._last_restart_attempt = now
        self._restart_count += 1
        label = self._stream_label()
        print(f"  [{label}] auto-restart #{self._restart_count}: {reason}")
        try:
            self.stop()
        except Exception as e:
            print(f"  [{label}] stop during restart failed: {type(e).__name__}: {e}")
        if not self.start():
            return False, "restart spawn failed"
        new_healthy, new_reason = self.health_status()
        if new_healthy:
            self._on_restart_success()
        return new_healthy, new_reason

    def _on_restart_success(self) -> None:
        """Hook for subclasses to run post-restart logic (e.g. reconciliation)."""
        pass

    # ───────── reader thread ─────────

    def _reader_loop(self) -> None:
        assert self._proc is not None
        label = self._stream_label()
        exit_reason = "EOF (subprocess closed stdout)"
        try:
            for raw in self._proc.stdout:  # type: ignore[union-attr]
                if self._shutdown.is_set():
                    exit_reason = "shutdown signal"
                    break
                line = raw.rstrip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except json.JSONDecodeError:
                    print(f"  [{label}] non-JSON line: {line[:120]}")
                    continue
                self._on_message(msg)
        except Exception as e:
            exit_reason = f"crashed: {type(e).__name__}: {e}"
            if not self._shutdown.is_set():
                print(f"  [{label}] reader thread error: {type(e).__name__}: {e}")
        finally:
            self._reader_exit_reason = exit_reason
            if not self._shutdown.is_set():
                print(f"  [{label}] reader thread exited: {exit_reason}")

    def _stderr_loop(self) -> None:
        if self._proc is None or self._proc.stderr is None:
            return
        label = self._stream_label()
        try:
            for raw in self._proc.stderr:  # type: ignore[union-attr]
                if self._shutdown.is_set():
                    break
                line = raw.rstrip()
                if line:
                    print(f"  [{label} stderr] {line[:200]}")
        except Exception as e:
            if not self._shutdown.is_set():
                print(f"  [{label}] stderr reader error: {type(e).__name__}: {e}")


# ═══════════════════════════════════════════════════════════════
# CANDLE STREAM — kraken ws ohlc push-based candle updates
# ═══════════════════════════════════════════════════════════════

class CandleStream(BaseStream):
    """Push-based OHLC candle stream. Subscribes to all traded pairs in one
    WS connection, stores the latest candle per pair, and exposes it via
    latest_candle(pair). Falls back to REST ohlc() when unhealthy."""

    # Reverse map: WS symbol (e.g. "SOL/USDC", "SOL/BTC") → friendly pair.
    # Built dynamically from the pairs list at init.

    def __init__(self, pairs: List[str], interval: int = 5, paper: bool = False):
        super().__init__(paper=paper)
        self._pairs = list(pairs)
        self._interval = interval
        self._latest: Dict[str, dict] = {}
        # Build symbol → friendly pair reverse map.
        # WS v2 returns symbols like "SOL/BTC", "BTC/USDC" (canonical names).
        self._symbol_map: Dict[str, str] = {}
        for p in pairs:
            ws_name = KrakenCLI._resolve_ws_pair(p)
            self._symbol_map[p] = p
            self._symbol_map[ws_name] = p

    def _build_cmd(self) -> str:
        ws_pairs = [KrakenCLI._resolve_ws_pair(p) for p in self._pairs]
        pairs_str = " ".join(ws_pairs)
        return (f"exec kraken ws ohlc {pairs_str} "
                f"--interval {self._interval} -o json --snapshot true")

    def _stream_label(self) -> str:
        return "CANDLE_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "ohlc":
            # status, subscribe confirmations — bump heartbeat on status
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [CANDLE_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol", "")
            pair = self._symbol_map.get(symbol)
            if pair:
                with self._lock:
                    self._latest[pair] = entry

    def latest_candle(self, pair: str) -> Optional[Dict[str, Any]]:
        """Return the most recent candle for the given pair, or None."""
        with self._lock:
            return self._latest.get(pair)


# ═══════════════════════════════════════════════════════════════
# TICKER STREAM — kraken ws ticker push-based price updates
# ═══════════════════════════════════════════════════════════════

class TickerStream(BaseStream):
    """Push-based ticker stream. Subscribes to all traded pairs in one WS
    connection, stores the latest ticker per pair, and exposes it via
    latest_ticker(pair). Falls back to REST ticker() when unhealthy."""

    def __init__(self, pairs: List[str], paper: bool = False):
        super().__init__(paper=paper)
        self._pairs = list(pairs)
        self._latest: Dict[str, dict] = {}
        self._symbol_map: Dict[str, str] = {}
        for p in pairs:
            ws_name = KrakenCLI._resolve_ws_pair(p)
            self._symbol_map[p] = p
            self._symbol_map[ws_name] = p

    def _build_cmd(self) -> str:
        ws_pairs = [KrakenCLI._resolve_ws_pair(p) for p in self._pairs]
        pairs_str = " ".join(ws_pairs)
        return f"exec kraken ws ticker {pairs_str} -o json --snapshot true"

    def _stream_label(self) -> str:
        return "TICKER_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "ticker":
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [TICKER_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol", "")
            pair = self._symbol_map.get(symbol)
            if pair:
                with self._lock:
                    self._latest[pair] = entry

    def latest_ticker(self, pair: str) -> Optional[Dict[str, Any]]:
        """Return the most recent ticker for the given pair, or None."""
        with self._lock:
            return self._latest.get(pair)


# ═══════════════════════════════════════════════════════════════
# BOOK STREAM — kraken ws book push-based order book updates
# ═══════════════════════════════════════════════════════════════

class BookStream(BaseStream):
    """Push-based order book stream. Subscribes to all traded pairs in one WS
    connection, stores the latest book per pair, and exposes it via
    latest_book(pair) in the REST-compatible format that OrderBookAnalyzer
    expects: {"bids": [[price, qty, ts], ...], "asks": [[price, qty, ts], ...]}.

    WS book snapshots include a checksum for integrity; we store the raw
    snapshot/update data and convert to REST format on read."""

    def __init__(self, pairs: List[str], depth: int = 10, paper: bool = False):
        super().__init__(paper=paper)
        self._pairs = list(pairs)
        self._depth = depth
        self._latest: Dict[str, dict] = {}
        self._symbol_map: Dict[str, str] = {}
        for p in pairs:
            ws_name = KrakenCLI._resolve_ws_pair(p)
            self._symbol_map[p] = p
            self._symbol_map[ws_name] = p

    def _build_cmd(self) -> str:
        ws_pairs = [KrakenCLI._resolve_ws_pair(p) for p in self._pairs]
        pairs_str = " ".join(ws_pairs)
        return (f"exec kraken ws book {pairs_str} "
                f"--depth {self._depth} -o json --snapshot true")

    def _stream_label(self) -> str:
        return "BOOK_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "book":
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [BOOK_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            symbol = entry.get("symbol", "")
            pair = self._symbol_map.get(symbol)
            if not pair:
                continue
            # Convert WS format {price, qty} dicts to REST format [price, qty, 0]
            # so OrderBookAnalyzer works unchanged.
            bids = []
            for b in entry.get("bids", []):
                if isinstance(b, dict):
                    bids.append([float(b.get("price", 0)), float(b.get("qty", 0)), 0])
            asks = []
            for a in entry.get("asks", []):
                if isinstance(a, dict):
                    asks.append([float(a.get("price", 0)), float(a.get("qty", 0)), 0])
            with self._lock:
                self._latest[pair] = {"bids": bids, "asks": asks}

    def latest_book(self, pair: str) -> Optional[Dict[str, Any]]:
        """Return the latest order book for the pair in REST-compatible format,
        or None if no data available."""
        with self._lock:
            return self._latest.get(pair)


# ═══════════════════════════════════════════════════════════════
# BALANCE STREAM — kraken ws balances push-based balance updates
# ═══════════════════════════════════════════════════════════════

class BalanceStream(BaseStream):
    """Push-based balance stream. Receives real-time balance updates for all
    assets. latest_balances() returns {asset: amount} for non-zero currency
    balances, matching the shape of KrakenCLI.balance().

    WS returns asset names like "BTC", "USD", "USDC", "SOL" etc.
    We normalize via KrakenCLI._normalize_asset so callers see canonical names.
    Only currency assets are included (equities/ETFs filtered out)."""

    def __init__(self, paper: bool = False):
        super().__init__(paper=paper)
        self._balances: Dict[str, float] = {}

    def _build_cmd(self) -> str:
        return "exec kraken ws balances -o json --snapshot true"

    def _stream_label(self) -> str:
        return "BALANCE_WS"

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel != "balances":
            if channel == "status":
                return
            if msg.get("method") == "subscribe":
                if not msg.get("success"):
                    print(f"  [BALANCE_WS] subscribe failed: {msg}")
                return
            return
        self._on_heartbeat()
        for entry in msg.get("data", []):
            if not isinstance(entry, dict):
                continue
            # Only include currency assets (skip equities/ETFs)
            if entry.get("asset_class", "currency") != "currency":
                continue
            asset = entry.get("asset", "")
            balance = entry.get("balance")
            if not asset or balance is None:
                continue
            normalized = KrakenCLI._normalize_asset(asset)
            with self._lock:
                bal = float(balance)
                if bal > 0:
                    self._balances[normalized] = bal
                else:
                    self._balances.pop(normalized, None)

    def latest_balances(self) -> Dict[str, float]:
        """Return {asset: amount} for non-zero currency balances."""
        with self._lock:
            return dict(self._balances)


# ═══════════════════════════════════════════════════════════════
# EXECUTION STREAM — kraken ws executions push reconciler
# ═══════════════════════════════════════════════════════════════

def _is_fully_filled(vol_exec: float, placed: float, tolerance: float = 0.01) -> bool:
    """Shared fill-detection: True if vol_exec is within `tolerance` (1%)
    of the placed amount. Used by ExecutionStream, restart-gap reconciliation,
    and resume reconciliation so all paths agree."""
    if placed <= 0:
        return False
    return abs(vol_exec - placed) / placed < tolerance


class ExecutionStream(BaseStream):
    """Consumes `kraken ws executions` and delivers push-based lifecycle
    events to the agent tick loop.

    Correlation keys: order_id (from REST placement response) is primary;
    order_userref (numeric tag we passed on placement) is fallback. Both
    are checked — whichever arrives first resolves the match.

    Paper mode uses paper=True which short-circuits start() and lets the
    place_order helper emit synthetic terminal events directly into the
    event queue. No subprocess is spawned.
    """

    def __init__(self, paper: bool = False):
        super().__init__(paper=paper)
        self._event_queue: "queue.Queue[tuple]" = queue.Queue()
        self._known_orders: Dict[str, dict] = {}
        self._userref_to_order_id: Dict[int, str] = {}
        self._last_sequence: Optional[int] = None
        self._pending_reconciliation: List[Dict[str, Any]] = []

    def _build_cmd(self) -> str:
        return ("exec kraken ws executions -o json "
                "--snap-orders true --snap-trades true")

    def _stream_label(self) -> str:
        return "EXECSTREAM"

    def _on_start_reset(self) -> None:
        # Reset sequence on (re)start — new WS connection starts at seq 1.
        # _known_orders intentionally NOT cleared — in-flight orders must
        # survive restarts for snapshot replay to finalize them.
        self._last_sequence = None

    def _on_message(self, msg: Dict[str, Any]) -> None:
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._on_heartbeat()
            return
        if channel == "status":
            return
        if msg.get("method") == "subscribe":
            if not msg.get("success"):
                print(f"  [EXECSTREAM] subscribe failed: {msg}")
            return
        if channel != "executions":
            return
        self._on_heartbeat()
        seq = msg.get("sequence")
        if isinstance(seq, int):
            if self._last_sequence is not None and seq != self._last_sequence + 1:
                print(
                    f"  [EXECSTREAM] sequence gap {self._last_sequence}->{seq} "
                    f"(executions may have been dropped; waiting for next snapshot)"
                )
            self._last_sequence = seq
        msg_type = msg.get("type")
        data = msg.get("data") or []
        if not isinstance(data, list):
            return
        for entry in data:
            if isinstance(entry, dict):
                self._event_queue.put((msg_type or "update", entry))

    def _on_restart_success(self) -> None:
        try:
            gap_events = self.reconcile_restart_gap()
            if gap_events:
                self._pending_reconciliation.extend(gap_events)
        except Exception as e:
            print(f"  [EXECSTREAM] restart-gap reconcile failed: {type(e).__name__}: {e}")

    # ───────── restart-gap reconciliation ─────────

    def reconcile_restart_gap(self) -> List[Dict[str, Any]]:
        """Query Kraken for orders in _known_orders that may have filled or
        cancelled while the execution stream was down."""
        if self.paper or not self._known_orders:
            return []

        with self._lock:
            order_ids = [oid for oid in self._known_orders if oid != "unknown"]
        if not order_ids:
            return []

        terminal_events: List[Dict[str, Any]] = []
        BATCH = 20

        for i in range(0, len(order_ids), BATCH):
            batch = order_ids[i:i + BATCH]
            time.sleep(2)
            resp = KrakenCLI.query_orders(*batch, trades=True)
            if not isinstance(resp, dict) or "error" in resp:
                continue

            for txid, order_info in resp.items():
                if not isinstance(order_info, dict):
                    continue
                with self._lock:
                    known = self._known_orders.get(txid)
                if not known:
                    continue

                status = order_info.get("status", "")
                if status not in ("closed", "canceled", "expired"):
                    continue

                vol_exec = float(order_info.get("vol_exec", 0))
                placed = known["placed_amount"]
                raw_price = float(order_info.get("price", 0))
                avg_price = raw_price if raw_price > 0 else None
                fee = float(order_info.get("fee", 0))

                if status == "closed":
                    state = (
                        "FILLED"
                        if _is_fully_filled(vol_exec, placed)
                        else "PARTIALLY_FILLED"
                    )
                elif vol_exec > 0:
                    state = "PARTIALLY_FILLED"
                else:
                    state = "CANCELLED_UNFILLED"

                event = {
                    "order_id": txid,
                    "journal_index": known["journal_index"],
                    "engine_ref": known["engine_ref"],
                    "pre_trade_snapshot": known["pre_trade_snapshot"],
                    "placed_amount": placed,
                    "pair": known["pair"],
                    "side": known["side"],
                    "state": state,
                    "vol_exec": vol_exec,
                    "avg_fill_price": avg_price,
                    "fee_quote": fee,
                    "terminal_reason": f"reconciled after stream restart ({status})",
                    "exec_ids": [],
                    "timestamp": order_info.get("closetm") or order_info.get("opentm"),
                }
                terminal_events.append(event)
                with self._lock:
                    self._known_orders.pop(txid, None)

        if terminal_events:
            print(f"  [EXECSTREAM] reconciled {len(terminal_events)} order(s) after restart gap")
        return terminal_events

    # ───────── registration ─────────

    def register(self, *, order_id: str, userref: Optional[int],
                 journal_index: int, pair: str, side: str,
                 placed_amount: float, engine_ref: Any,
                 pre_trade_snapshot: Any) -> None:
        """Correlate an in-flight placement with its journal entry and
        rollback handle. Skips registration when order_id is 'unknown'
        (REST returned no txid) — such orders can't be tracked by id and
        won't finalize via this stream; the placement helper should log
        a warning in that case."""
        if not order_id or order_id == "unknown":
            return
        with self._lock:
            self._known_orders[order_id] = {
                "order_id": order_id,
                "userref": userref,
                "journal_index": journal_index,
                "pair": pair,
                "side": side,
                "placed_amount": float(placed_amount),
                "engine_ref": engine_ref,
                "pre_trade_snapshot": pre_trade_snapshot,
                "registered_at": time.time(),
                "vol_exec_running": 0.0,
                "cost_running": 0.0,
                "fee_running": 0.0,
                "exec_ids": [],
            }
            if userref is not None:
                self._userref_to_order_id[int(userref)] = order_id

    def inject_event(self, entry: Dict[str, Any], *, kind: str = "update") -> None:
        """Test/paper hook: push an execution entry straight into the queue
        without going through the subprocess. Used by paper mode to synthesize
        fill events and by FakeExecutionStream in tests."""
        self._event_queue.put((kind, entry))

    # ───────── consumption ─────────

    # Terminal Kraken order_status values
    _TERMINAL_STATUSES = {"filled", "canceled", "expired", "rejected"}

    def drain_events(self) -> List[Dict[str, Any]]:
        """Called once per tick. Pops every queued WS entry, updates the
        per-order aggregator, and emits one terminal event per order that
        finished this drain. Non-terminal updates (pending_new, new,
        interim partial fills) update internal state silently.

        Returned event shape (flat dict, agent applies directly to journal
        + engine state):

            {
                "order_id":          str,
                "journal_index":     int,
                "engine_ref":        HydraEngine,
                "pre_trade_snapshot": dict,
                "placed_amount":     float,
                "pair":              str,
                "side":              "BUY" | "SELL",
                "state":             "FILLED" | "PARTIALLY_FILLED" |
                                     "CANCELLED_UNFILLED" | "REJECTED",
                "vol_exec":          float,
                "avg_fill_price":    Optional[float],
                "fee_quote":         float,
                "terminal_reason":   Optional[str],
                "exec_ids":          List[str],
                "timestamp":         Optional[str],
            }
        """
        events: List[Dict[str, Any]] = []
        # Prepend any events from restart-gap reconciliation so the agent
        # processes them in the same tick the stream recovered.
        if self._pending_reconciliation:
            events.extend(self._pending_reconciliation)
            self._pending_reconciliation.clear()
        while True:
            try:
                _kind, entry = self._event_queue.get_nowait()
            except queue.Empty:
                break
            term = self._apply_entry(entry)
            if term is not None:
                events.append(term)
        return events

    def _apply_entry(self, entry: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        """Fold one WS execution entry into the per-order aggregate. Returns
        a terminal event if the order finalized on this entry, else None."""
        order_id = entry.get("order_id")
        userref = entry.get("order_userref")
        with self._lock:
            known: Optional[dict] = None
            if isinstance(order_id, str) and order_id in self._known_orders:
                known = self._known_orders[order_id]
            elif isinstance(userref, int) and userref in self._userref_to_order_id:
                resolved_id = self._userref_to_order_id[userref]
                known = self._known_orders.get(resolved_id)
                if known is not None:
                    order_id = resolved_id
            if known is None:
                # Not one of ours (snapshot of historical fills, manual trade,
                # or an order that hasn't been register()'d yet due to a race).
                return None

            order_status = entry.get("order_status")

            # Fold trade/fill events into the running totals. Don't gate on
            # exec_type — it's purely labeling (observed "trade" in the v2
            # snapshot). Trust last_qty + last_price to detect a real fill.
            last_qty = entry.get("last_qty")
            last_price = entry.get("last_price")
            if isinstance(last_qty, (int, float)) and last_qty > 0:
                last_qty_f = float(last_qty)
                last_price_f = float(last_price) if isinstance(last_price, (int, float)) else 0.0
                cost_raw = entry.get("cost")
                cost_f = float(cost_raw) if isinstance(cost_raw, (int, float)) else (last_qty_f * last_price_f)
                fees = entry.get("fees") or []
                fee_delta = 0.0
                if isinstance(fees, list):
                    for fee in fees:
                        if isinstance(fee, dict):
                            q = fee.get("qty")
                            if isinstance(q, (int, float)):
                                fee_delta += float(q)
                known["vol_exec_running"] += last_qty_f
                known["cost_running"] += cost_f
                known["fee_running"] += fee_delta
                exec_id = entry.get("exec_id")
                if isinstance(exec_id, str) and exec_id:
                    known["exec_ids"].append(exec_id)

            # Only emit a terminal event once the order reaches a terminal
            # order_status. exec_type alone is not enough — a "trade" exec
            # can be interim on a partially-filled order still open.
            if order_status not in self._TERMINAL_STATUSES:
                return None

            vol_exec = known["vol_exec_running"]
            placed = known["placed_amount"]
            avg_price = (known["cost_running"] / vol_exec) if vol_exec > 0 else None

            if order_status == "filled":
                if _is_fully_filled(vol_exec, placed):
                    state = "FILLED"
                else:
                    state = "PARTIALLY_FILLED"
                terminal_reason: Optional[str] = None
            elif order_status in ("canceled", "expired"):
                reason = entry.get("reason") or order_status
                terminal_reason = str(reason)
                if vol_exec <= 0:
                    state = "CANCELLED_UNFILLED"
                else:
                    state = "PARTIALLY_FILLED"
            elif order_status == "rejected":
                state = "REJECTED"
                terminal_reason = str(entry.get("reason") or "rejected")
            else:
                return None  # unreachable given _TERMINAL_STATUSES guard

            term = {
                "order_id": known["order_id"],
                "journal_index": known["journal_index"],
                "engine_ref": known["engine_ref"],
                "pre_trade_snapshot": known["pre_trade_snapshot"],
                "placed_amount": placed,
                "pair": known["pair"],
                "side": known["side"],
                "state": state,
                "vol_exec": vol_exec,
                "avg_fill_price": avg_price,
                "fee_quote": known["fee_running"],
                "terminal_reason": terminal_reason,
                "exec_ids": list(known["exec_ids"]),
                "timestamp": entry.get("timestamp"),
            }

            # Drop from known maps — terminal means done.
            self._known_orders.pop(known["order_id"], None)
            uref = known.get("userref")
            if isinstance(uref, int):
                self._userref_to_order_id.pop(uref, None)
            return term


class FakeExecutionStream(ExecutionStream):
    """Test/harness double: identical interface, no subprocess, no thread.

    Tests push synthetic WS execution entries via `inject_event(...)` and
    then call `drain_events()` to collect terminal events. Used by the
    live harness in mock mode so scenario runs stay fast and hermetic."""

    def __init__(self):
        super().__init__(paper=False)
        # Override so healthy reports True without a subprocess
        self._fake_healthy = True
        self._last_heartbeat = time.monotonic()

    def start(self) -> bool:
        # No-op — tests drive events via inject_event.
        return True

    def stop(self) -> None:
        self._shutdown.set()

    @property
    def healthy(self) -> bool:
        return self._fake_healthy

    def health_status(self) -> Tuple[bool, str]:
        if self._fake_healthy:
            return True, ""
        return False, "fake stream marked unhealthy"

    def ensure_healthy(self) -> Tuple[bool, str]:
        # Tests are deterministic — never auto-restart, just report.
        return self.health_status()

    def set_healthy(self, value: bool) -> None:
        self._fake_healthy = value


class FakeTickerStream(TickerStream):
    """Test double for TickerStream — no subprocess, returns injected data."""

    def __init__(self, pairs, **kw):
        super().__init__(pairs=pairs, paper=True)
        self._healthy = True

    def start(self):
        return True

    def stop(self):
        pass

    @property
    def healthy(self):
        return self._healthy

    def health_status(self):
        return (self._healthy, "fake" if self._healthy else "fake_unhealthy")

    def ensure_healthy(self):
        # Match BaseStream contract: return (healthy, reason) tuple rather
        # than None. Tests are deterministic; never auto-restart.
        return self.health_status()

    def set_healthy(self, h):
        self._healthy = h

    def inject(self, pair, data):
        """Inject ticker data for a pair (bypasses WS symbol mapping)."""
        with self._lock:
            self._latest[pair] = data


# ═══════════════════════════════════════════════════════════════
# HYDRA AGENT (Main Loop)
# ═══════════════════════════════════════════════════════════════

class HydraAgent:
    """
    Main agent loop. Fetches live data from Kraken CLI, feeds it to the
    engine, executes real trades, and broadcasts state to the dashboard.
    """

    # Pair configuration
    PRIMARY_PAIR = "SOL/USDC"       # Main trading pair
    CROSS_PAIR = "SOL/BTC"          # Opportunistic regime-driven swaps
    BTC_PAIR = "BTC/USDC"           # BTC priced in stablecoin
    ORDER_JOURNAL_CAP = 2000        # Bound in-memory order journal
    SNAPSHOT_EVERY_N_TICKS = 120    # ~10h at 300s ticks (also triggers immediately on journal writes)

    def __init__(
        self,
        pairs: List[str],
        initial_balance: float = 100.0,
        interval_seconds: int = 60,
        duration_seconds: int = 600,
        ws_port: int = 8765,
        mode: str = "conservative",
        paper: bool = False,
        candle_interval: int = 15,
        reset_params: bool = False,
        resume: bool = False,
    ):
        self.pairs = pairs
        self.initial_balance = initial_balance
        self._competition_start_balance = None  # Set once on first start, persisted across resumes
        self.interval = interval_seconds
        self.duration = duration_seconds
        self.mode = mode
        self.paper = paper
        self.candle_interval = candle_interval
        self.running = True
        self.start_time = None
        self.order_journal: List[Dict[str, Any]] = []
        self._snapshot_dir = os.path.dirname(os.path.abspath(__file__))
        self._completed_trades_since_update = 0  # Counter for tuner update cadence
        self._last_brain_candle_ts: Dict[str, float] = {}  # Per-pair: last candle timestamp brain evaluated
        self._last_ai_decision: Dict[str, Dict] = {}         # Per-pair: last brain decision for dashboard persistence
        # Portfolio-level awareness
        self._current_portfolio_summary: Dict[str, Any] = {}  # Aggregate stats computed each tick
        self._portfolio_guidance: Optional[str] = None         # Latest Grok portfolio assessment text
        self._portfolio_candle_epoch: Dict[str, float] = {}    # Per-pair candle ts for epoch tracking
        self._portfolio_epoch_count: int = 0                   # Epochs since last portfolio review
        self._last_portfolio_review_regimes: Dict[str, str] = {}  # Regimes at last review
        # Monotonic client tag seeded from wall-clock to avoid collisions
        # across restarts; flows into Kraken as --userref and comes back on
        # the WS executions stream as order_userref for correlation.
        #
        # This initial time-seed is a floor — after snapshot load and journal
        # merge, _reseed_userref_from_history() raises it above anything we've
        # used in the past. Without that, a restart within the same second as
        # a killed session could collide with still-open orders' userrefs.
        self._userref_counter = int(time.time()) & 0x7FFFFFFF

        # Sizing config based on mode
        sizing = SIZING_COMPETITION if mode == "competition" else SIZING_CONSERVATIVE

        # Self-tuning parameter trackers (one per pair)
        base_dir = os.path.dirname(os.path.abspath(__file__))
        self.trackers: Dict[str, ParameterTracker] = {}
        for pair in pairs:
            tracker = ParameterTracker(pair=pair, save_dir=base_dir)
            if reset_params:
                tracker.reset()
                print(f"  [TUNER] Reset learned params for {pair}")
            self.trackers[pair] = tracker

        # One engine per pair — apply tuned params if available
        self.engines: Dict[str, HydraEngine] = {}
        for pair in pairs:
            # Volatility thresholds are now adaptive (multiplier on median
            # ATR%) — no candle-interval branching needed; the median
            # self-adjusts for wider candle bars.
            self.engines[pair] = HydraEngine(
                initial_balance=initial_balance / len(pairs),
                asset=pair,
                sizing=sizing,
                candle_interval=candle_interval,
            )
            # Apply any previously learned tuned params
            tuned = self.trackers[pair].get_tunable_params()
            self.engines[pair].apply_tuned_params(tuned)
            if self.trackers[pair].update_count > 0:
                print(f"  [TUNER] {pair}: loaded tuned params (update #{self.trackers[pair].update_count})")

        # Dashboard broadcaster
        self.broadcaster = DashboardBroadcaster(port=ws_port)

        # ─── Thesis layer (v2.13.0, Phase A — Golden Unicorn) ──────────
        # Slow-moving persistent worldview + user-authored intent. Phase A
        # is surface-only: state + knobs load/save, dashboard THESIS tab,
        # WS handlers. No brain wiring, no signal gating, no ladders —
        # those land in Phases B–E. Kill-switchable via HYDRA_THESIS_DISABLED=1
        # (drift regression test enforces v2.12.5 bit-identical behavior
        # when disabled). Any init failure leaves the live agent untouched.
        self.thesis = None
        self.thesis_processor = None
        try:
            self.thesis = ThesisTracker.load_or_default(save_dir=base_dir)
            if self.thesis.disabled:
                print("  [THESIS] subsystem disabled via HYDRA_THESIS_DISABLED=1")
            else:
                print(f"  [THESIS] layer loaded (posture={self.thesis.posture})")
        except Exception as e:
            print(f"  [THESIS] init failed ({type(e).__name__}: {e}); disabled for this run")
            self.thesis = ThesisTracker(save_dir=base_dir, disabled=True)

        # v2.13.2 (Phase C): Grok document processor. Available only when
        # XAI_API_KEY is set AND HYDRA_THESIS_PROCESSOR_DISABLED != 1 AND
        # the thesis layer itself is enabled. Daemon worker; failure
        # isolation mirrors the backtest subsystem.
        try:
            if (self.thesis and not self.thesis.disabled
                    and not os.environ.get("HYDRA_THESIS_PROCESSOR_DISABLED")):
                xai_key = os.environ.get("XAI_API_KEY", "")
                if xai_key:
                    budget = float(
                        (self.thesis.knobs or {}).get("grok_processing_budget_usd_per_day")
                        or 5.0
                    )
                    self.thesis_processor = ThesisProcessorWorker(
                        xai_key=xai_key,
                        pending_dir=self.thesis._pending_dir(),
                        get_thesis_state=lambda: self.thesis.snapshot(),
                        on_proposal=self._on_thesis_proposal,
                        broadcast=self.broadcaster.broadcast_message,
                        daily_budget_usd=budget,
                    )
                    if self.thesis_processor.available:
                        self.thesis_processor.start()
                        print(f"  [THESIS_PROC] Grok document processor started (budget=${budget:.2f}/day)")
                    else:
                        print("  [THESIS_PROC] worker unavailable (openai client unreachable)")
                else:
                    print("  [THESIS_PROC] XAI_API_KEY not set — processor offline")
        except Exception as e:
            print(f"  [THESIS_PROC] init failed ({type(e).__name__}: {e}); disabled for this run")
            self.thesis_processor = None

        # ─── Backtest subsystem (v2.10.0, Phase 6) ─────────────────────
        # Strictly additive. Kill-switchable via HYDRA_BACKTEST_DISABLED=1
        # (I6). Any failure inside init leaves the live agent completely
        # unaffected — we swallow + log, never raise.
        self.backtest_pool = None
        self.backtest_dispatcher = None
        if not os.environ.get("HYDRA_BACKTEST_DISABLED"):
            try:
                from hydra_backtest_server import (
                    BacktestWorkerPool, mount_backtest_routes,
                )
                from hydra_backtest_tool import BacktestToolDispatcher
                from hydra_experiments import ExperimentStore
                bt_store = ExperimentStore()
                self.backtest_dispatcher = BacktestToolDispatcher(store=bt_store)
                self.backtest_pool = BacktestWorkerPool(
                    max_workers=2,
                    store=bt_store,
                    broadcaster=self.broadcaster,
                )
                mount_backtest_routes(
                    self.broadcaster, self.backtest_pool,
                    dispatcher=self.backtest_dispatcher,
                )
                print("  [BACKTEST] subsystem mounted (max_workers=2)")
            except Exception as e:
                print(f"  [BACKTEST] init failed ({type(e).__name__}: {e}); disabled for this run")
                self.backtest_pool = None
                self.backtest_dispatcher = None

        # AI Brain (optional — Claude for analysis, Grok for strategic depth)
        self.brain = None
        if HAS_BRAIN:
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            xai_key = os.environ.get("XAI_API_KEY", "")
            if anthropic_key or openai_key or xai_key:
                try:
                    self.brain = HydraBrain(
                        anthropic_key=anthropic_key, openai_key=openai_key,
                        xai_key=xai_key,
                        tool_dispatcher=self.backtest_dispatcher,
                        # Gating stays env-driven (HYDRA_BRAIN_TOOLS_ENABLED=1)
                        # so brain tool-use is off by default even when the
                        # subsystem is mounted. Phase 12 flips the default.
                    )
                except Exception as e:
                    print(f"  [WARN] Brain init failed: {e}")

        # ─── Companion subsystem (v2.10.3+) ────────────────────────────
        # Strictly additive. Off unless HYDRA_COMPANION_ENABLED=1.
        # Kill switch: HYDRA_COMPANION_DISABLED=1 wins over all.
        # Any init failure leaves the live agent completely unaffected.
        self.companion_coordinator = None
        try:
            from hydra_companions.config import is_enabled as _comp_enabled
            if _comp_enabled():
                from hydra_companions.coordinator import CompanionCoordinator
                from hydra_companions.ws_handlers import mount_companion_routes
                self.companion_coordinator = CompanionCoordinator(self)
                mount_companion_routes(self.broadcaster, self.companion_coordinator)
                print("  [COMPANION] subsystem mounted (Athena, Apex, Broski)")
        except Exception as e:
            print(f"  [COMPANION] init failed ({type(e).__name__}: {e}); disabled for this run")
            self.companion_coordinator = None

        # v2.13.0: Mount Thesis WS handlers so the dashboard THESIS tab can
        # read/update knobs, posture, and hard rules. All handlers are no-ops
        # when the tracker is disabled (they report disabled:true back to UI
        # so the tab can render a clear "kill-switched" state).
        try:
            self._mount_thesis_routes()
        except Exception as e:
            print(f"  [THESIS] route mount failed ({type(e).__name__}: {e})")

        # Cross-pair regime coordinator
        self.coordinator = CrossPairCoordinator(pairs)
        self._swap_counter = 0  # Monotonic swap ID generator

        # Execution stream — push-based reconciler backed by `kraken ws
        # executions`. Paper mode short-circuits the subprocess and uses
        # synthetic fill events (inject_event) so the same code path
        # handles both real and paper flows.
        self.execution_stream = ExecutionStream(paper=paper)
        # Push-based market data streams — candle + ticker. Each subscribes
        # to all pairs in one WS connection. Paper mode short-circuits to
        # no-op (REST fallback used instead).
        self.candle_stream = CandleStream(pairs, interval=candle_interval, paper=paper)
        self.ticker_stream = TickerStream(pairs, paper=paper)
        self.balance_stream = BalanceStream(paper=paper)
        self.book_stream = BookStream(pairs, depth=10, paper=paper)
        # Tracks the most recently logged unhealthy reason so the tick body
        # only prints on transitions instead of spamming the warning every
        # tick. None means "currently healthy or never warned".
        self._exec_stream_warned_reason: Optional[str] = None

        # Kraken system status — tracks last known status for transition logging.
        # None means "never checked". Only checked in live mode.
        self._last_kraken_status: Optional[str] = None

        # Fee tier cache — refreshed at most once per hour from `kraken volume`.
        # Shape: {"volume_30d_usd": float|None, "pair_fees": {pair: {"maker_pct","taker_pct"}}}
        self._fee_tier_cache: dict = {}
        self._fee_tier_fetched_at: float = 0.0

        # Track previous regime for cross-pair swap triggers
        self.prev_regimes: Dict[str, str] = {}

        # Run the one-shot legacy trade_log -> order_journal migration
        # before touching any on-disk state. Idempotent; no-op after the
        # first run. Lives in hydra_journal_migrator so it can be invoked
        # standalone as well.
        try:
            migrate_legacy_trade_log_file(self._snapshot_dir, verbose=False)
        except Exception as e:
            print(f"  [MIGRATE] legacy journal migration skipped: {e}")

        # Restore from snapshot if requested
        if resume:
            self._load_snapshot()

        # Merge the on-disk rolling journal into self.order_journal regardless
        # of --resume. The snapshot only holds the last 200 entries; the
        # rolling file is the long-horizon record. Prior versions would
        # overwrite the rolling file on the first tick after restart,
        # truncating history — this merges it in first so restarts preserve
        # full depth (bounded by ORDER_JOURNAL_CAP).
        self._merge_order_journal()

        # Reseed _userref_counter above anything we've used historically.
        # Must run AFTER both _load_snapshot (may carry a persisted counter)
        # AND _merge_order_journal (gives us the historical high-water mark).
        self._reseed_userref_from_history()

        # Graceful shutdown
        sig.signal(sig.SIGINT, self._handle_shutdown)
        sig.signal(sig.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        print("\n\n  [HYDRA] Shutdown signal received. Cancelling orders, flushing snapshot...\n")
        self.running = False
        # Cancel all resting limit orders on the exchange (live mode only)
        if not self.paper:
            try:
                result = KrakenCLI.cancel_all()
                if "error" in result:
                    print(f"  [HYDRA] Cancel-all error: {result['error']}")
                else:
                    print("  [HYDRA] All open orders cancelled.")
            except Exception as e:
                print(f"  [HYDRA] Cancel-all failed: {e}")
        # Tear down all WS stream subprocesses
        for stream, label in [
            (self.execution_stream, "ExecutionStream"),
            (self.candle_stream, "CandleStream"),
            (self.ticker_stream, "TickerStream"),
            (self.balance_stream, "BalanceStream"),
            (self.book_stream, "BookStream"),
        ]:
            try:
                stream.stop()
            except Exception as e:
                print(f"  [HYDRA] {label} stop failed: {e}")
        # Drain the backtest worker pool (daemon threads — best-effort join).
        if self.backtest_pool is not None:
            try:
                self.backtest_pool.shutdown(timeout=3.0)
            except Exception as e:
                print(f"  [HYDRA] Backtest pool shutdown failed: {e}")
        # Flush session snapshot for --resume
        try:
            self._save_snapshot()
        except Exception as e:
            print(f"  [HYDRA] Snapshot flush failed: {e}")

    # ─── Session snapshot (atomic JSON; resumable across runs) ─────────────

    def _snapshot_path(self) -> str:
        return os.path.join(self._snapshot_dir, "hydra_session_snapshot.json")

    def _save_snapshot(self):
        """Atomically save session state to disk (.tmp -> os.replace)."""
        snapshot = {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "paper": self.paper,
            "pairs": self.pairs,
            "competition_start_balance": self._competition_start_balance,
            "engines": {pair: eng.snapshot_runtime() for pair, eng in self.engines.items()},
            "coordinator_regime_history": self.coordinator.regime_history,
            "order_journal": self.order_journal[-200:],
            # Persist the userref counter so a restart never re-issues a
            # userref already in-flight on the exchange from this session.
            "userref_counter": self._userref_counter,
            # v2.13.0: Thesis layer state. Empty dict when disabled — the
            # tracker's snapshot() returns {} so the load path is fail-soft.
            # getattr guards tests that use object.__new__(HydraAgent) to
            # bypass __init__ and therefore never set self.thesis.
            "thesis_state": (getattr(self, "thesis", None).snapshot()
                             if getattr(self, "thesis", None) else {}),
        }
        path = self._snapshot_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snapshot, f, default=str)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [SNAPSHOT] Save failed: {e}")

    @staticmethod
    def _normalize_pair_name(pair: str) -> str:
        """Normalize legacy XBT pair names to BTC canonical form.

        Handles snapshot/journal data written before the XBT→BTC migration.
        """
        if "XBT" not in pair:
            return pair
        return pair.replace("XBT/USDC", "BTC/USDC").replace("SOL/XBT", "SOL/BTC").replace("XBT/", "BTC/")

    @staticmethod
    def _normalize_journal_pairs(journal: list):
        """Normalize pair names in journal entries from XBT to BTC canonical."""
        for entry in journal:
            if isinstance(entry, dict) and "pair" in entry:
                entry["pair"] = HydraAgent._normalize_pair_name(entry["pair"])

    def _load_snapshot(self):
        """Restore engine + coordinator state from snapshot file."""
        path = self._snapshot_path()
        if not os.path.exists(path):
            print("  [SNAPSHOT] No snapshot file found, starting fresh.")
            return
        try:
            with open(path, "r") as f:
                snapshot = json.load(f)
            if snapshot.get("version") != 1:
                print(f"  [SNAPSHOT] Unknown version {snapshot.get('version')}, skipping.")
                return
            # Normalize legacy XBT pair names in engine keys
            engines_raw = snapshot.get("engines", {})
            engines = {self._normalize_pair_name(k): v for k, v in engines_raw.items()}
            for pair, eng_snap in engines.items():
                if pair in self.engines:
                    self.engines[pair].restore_runtime(eng_snap)
            # Normalize coordinator regime history keys
            coord_raw = snapshot.get("coordinator_regime_history", {})
            for pair, history in coord_raw.items():
                norm_pair = self._normalize_pair_name(pair)
                if norm_pair in self.coordinator.regime_history:
                    self.coordinator.regime_history[norm_pair] = list(history)
            self.order_journal = list(snapshot.get("order_journal", []))
            self._normalize_journal_pairs(self.order_journal)
            if snapshot.get("competition_start_balance") is not None:
                self._competition_start_balance = float(snapshot["competition_start_balance"])
            # Carry the persisted userref floor into _userref_counter. The
            # _reseed_userref_from_history() call in __init__ will raise it
            # further if the journal reveals higher values.
            persisted_uref = snapshot.get("userref_counter")
            if isinstance(persisted_uref, int) and 0 < persisted_uref < (1 << 31):
                self._userref_counter = max(self._userref_counter, persisted_uref)
            # v2.13.0: Restore thesis layer state. Missing key (older snapshots)
            # or empty dict (disabled layer) both no-op inside tracker.restore().
            # getattr guards tests that use object.__new__(HydraAgent).
            thesis_attr = getattr(self, "thesis", None)
            if thesis_attr is not None:
                thesis_attr.restore(snapshot.get("thesis_state"))
            print(f"  [SNAPSHOT] Restored session from {snapshot.get('timestamp', '?')}")
        except Exception as e:
            print(f"  [SNAPSHOT] Load failed: {e}, starting fresh.")

    # ─── Thesis journal helpers (v2.13.1, Phase B) ────────────────────

    def _journal_thesis_posture(self) -> Optional[str]:
        """Posture stamp for journal entries — None when thesis disabled."""
        t = getattr(self, "thesis", None)
        if t is None or t.disabled:
            return None
        return t.posture

    def _journal_ladder_stamp(
        self, pair: str, side: str, price: Optional[float],
    ) -> Dict[str, Any]:
        """Compute the (ladder_id, rung_idx, adhoc) fields for a journal
        entry. Returns an empty dict when the thesis layer is disabled
        OR HYDRA_THESIS_LADDERS is unset, so entries from users who
        haven't opted in keep their v2.13.2 schema exactly."""
        t = getattr(self, "thesis", None)
        if t is None or t.disabled or not t._ladders_enabled():
            return {}
        if price is None:
            return {"ladder_id": None, "rung_idx": None, "adhoc": True}
        match = None
        try:
            match = t.match_rung(pair, side, price)
        except Exception as e:
            print(f"  [THESIS] match_rung error ({type(e).__name__}: {e})")
        if match:
            return {
                "ladder_id": match.get("ladder_id"),
                "rung_idx": match.get("rung_idx"),
                "adhoc": False,
            }
        return {"ladder_id": None, "rung_idx": None, "adhoc": True}

    def _journal_intents_active(self, ai: Optional[Dict[str, Any]]) -> Optional[List[str]]:
        """List of intent_ids the analyst consulted. Prefers the analyst's
        self-reported list (thesis_alignment.intent_prompts_consulted) — the
        agent doesn't second-guess the LLM's attribution. Returns None when
        thesis is disabled OR the analyst didn't report anything."""
        if not isinstance(ai, dict):
            return None
        t = getattr(self, "thesis", None)
        if t is None or t.disabled:
            return None
        ta = ai.get("thesis_alignment")
        if not isinstance(ta, dict):
            return None
        consulted = ta.get("intent_prompts_consulted") or []
        if not isinstance(consulted, list):
            return None
        return [str(x) for x in consulted]

    # ─── Thesis WS routes (v2.13.0, Phase A) ──────────────────────────
    # Handlers let the dashboard read/update knobs, posture, and hard rules.
    # Each handler broadcasts the new thesis_state so every connected client
    # stays in sync after a mutation. Disabled mode short-circuits to inert
    # responses so the UI can render a "kill-switched" banner.

    def _broadcast_thesis_state(self) -> None:
        """Push current thesis_state to all dashboard clients."""
        if not self.thesis:
            return
        try:
            self.broadcaster.broadcast_message(
                "thesis_state",
                {"data": self.thesis.current_state()},
            )
        except Exception as e:
            print(f"  [THESIS] broadcast failed: {type(e).__name__}: {e}")

    def _handle_thesis_get_state(self, payload: Dict[str, Any]) -> None:
        self._broadcast_thesis_state()

    def _handle_thesis_update_knobs(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        patch = (payload or {}).get("knobs") or {}
        self.thesis.update_knobs(patch)
        self._broadcast_thesis_state()

    def _handle_thesis_update_posture(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        posture = (payload or {}).get("posture")
        if posture:
            self.thesis.update_posture(posture)
        self._broadcast_thesis_state()

    def _handle_thesis_update_hard_rules(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        patch = (payload or {}).get("hard_rules") or {}
        self.thesis.update_hard_rules(patch)
        self._broadcast_thesis_state()

    def _handle_thesis_create_intent(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        p = payload or {}
        self.thesis.add_intent(
            prompt_text=p.get("prompt_text", ""),
            pair_scope=p.get("pair_scope"),
            priority=p.get("priority", 3),
            expires_at=p.get("expires_at"),
            author=p.get("author", "user"),
        )
        self._broadcast_thesis_state()

    def _handle_thesis_delete_intent(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        intent_id = (payload or {}).get("intent_id")
        if intent_id:
            self.thesis.remove_intent(intent_id)
        self._broadcast_thesis_state()

    def _handle_thesis_update_intent(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        p = payload or {}
        intent_id = p.get("intent_id")
        patch = p.get("patch") or {}
        if intent_id and patch:
            self.thesis.update_intent(intent_id, patch)
        self._broadcast_thesis_state()

    # ─── Thesis document + proposal handlers (v2.13.2, Phase C) ───

    def _on_thesis_proposal(self, proposal: Dict[str, Any]) -> None:
        """Callback invoked by ThesisProcessorWorker once Grok has produced
        a proposal. Write to hydra_thesis_pending/ and broadcast."""
        if not self.thesis:
            return
        self.thesis.write_pending_proposal(proposal)
        self._broadcast_thesis_state()

    def _handle_thesis_upload_document(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        p = payload or {}
        ref = self.thesis.upload_document(
            filename=p.get("filename", "note.md"),
            content=p.get("content", ""),
            doc_type=p.get("doc_type", "other"),
        )
        if ref and self.thesis_processor and self.thesis_processor.available:
            try:
                with open(ref["file_path"], "r", encoding="utf-8") as f:
                    text = f.read()
                self.thesis_processor.submit({
                    "doc_id": ref["doc_id"],
                    "filename": ref["filename"],
                    "doc_type": ref["doc_type"],
                    "text": text,
                })
            except Exception as e:
                print(f"  [THESIS] document submit failed ({type(e).__name__}: {e})")
        self._broadcast_thesis_state()

    def _handle_thesis_list_proposals(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        proposals = self.thesis.list_pending_proposals()
        try:
            self.broadcaster.broadcast_message(
                "thesis_proposals_list", {"data": proposals},
            )
        except Exception:
            pass

    def _handle_thesis_approve_proposal(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        p = payload or {}
        self.thesis.approve_proposal(p.get("proposal_id", ""), p.get("user_notes"))
        self._broadcast_thesis_state()

    def _handle_thesis_reject_proposal(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        p = payload or {}
        self.thesis.reject_proposal(p.get("proposal_id", ""), p.get("user_notes"))
        self._broadcast_thesis_state()

    # ─── Thesis ladder handlers (v2.13.3, Phase D) ────────────────

    def _handle_thesis_create_ladder(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        p = payload or {}
        try:
            total = float(p.get("total_size", 0) or 0)
        except (TypeError, ValueError):
            total = 0.0
        if total <= 0:
            self._broadcast_thesis_state()
            return
        self.thesis.create_ladder(
            pair=p.get("pair", ""),
            side=p.get("side", "BUY"),
            total_size=total,
            rungs_spec=p.get("rungs") or [],
            stop_loss_price=p.get("stop_loss_price"),
            expiry_hours=p.get("expiry_hours"),
            expiry_action=p.get("expiry_action", "cancel"),
            reasoning=p.get("reasoning", ""),
            creator=p.get("creator", "user:dashboard"),
        )
        self._broadcast_thesis_state()

    def _handle_thesis_cancel_ladder(self, payload: Dict[str, Any]) -> None:
        if not self.thesis:
            return
        lid = (payload or {}).get("ladder_id", "")
        if lid:
            self.thesis.cancel_ladder(lid)
        self._broadcast_thesis_state()

    def _mount_thesis_routes(self) -> None:
        """Wire thesis WS handlers into the broadcaster. Safe on repeat
        invocation — register_handler overwrites prior mappings."""
        self.broadcaster.register_handler("thesis_get_state", self._handle_thesis_get_state)
        self.broadcaster.register_handler("thesis_update_knobs", self._handle_thesis_update_knobs)
        self.broadcaster.register_handler("thesis_update_posture", self._handle_thesis_update_posture)
        self.broadcaster.register_handler("thesis_update_hard_rules", self._handle_thesis_update_hard_rules)
        # v2.13.1 (Phase B) — intent prompt CRUD.
        self.broadcaster.register_handler("thesis_create_intent", self._handle_thesis_create_intent)
        self.broadcaster.register_handler("thesis_delete_intent", self._handle_thesis_delete_intent)
        self.broadcaster.register_handler("thesis_update_intent", self._handle_thesis_update_intent)
        # v2.13.2 (Phase C) — document uploads + Grok proposal approval workflow.
        self.broadcaster.register_handler("thesis_upload_document", self._handle_thesis_upload_document)
        self.broadcaster.register_handler("thesis_list_proposals", self._handle_thesis_list_proposals)
        self.broadcaster.register_handler("thesis_approve_proposal", self._handle_thesis_approve_proposal)
        self.broadcaster.register_handler("thesis_reject_proposal", self._handle_thesis_reject_proposal)
        # v2.13.3 (Phase D) — ladder primitive. Journal stamping lands in
        # _place_order; rungs match on (pair, side, price) within tolerance.
        # Feature flag: HYDRA_THESIS_LADDERS=1 (otherwise match_rung is a no-op
        # and journal schema stays v2.13.2).
        self.broadcaster.register_handler("thesis_create_ladder", self._handle_thesis_create_ladder)
        self.broadcaster.register_handler("thesis_cancel_ladder", self._handle_thesis_cancel_ladder)

    def _merge_order_journal(self):
        """Merge on-disk journal files into self.order_journal.

        Sources (in order):
          1. hydra_order_journal.json — rolling file, authoritative long-
             horizon record.  _save_snapshot caps at [-200:] so the
             rolling file preserves depth across restarts.
          2. hydra_order_journal_backfill.json — optional one-shot file
             for manual trades placed outside Hydra.  Consumed and
             deleted after merge so entries are ingested exactly once.

        Dedup key is (placed_at, order_id) when a Kraken order_id is
        available, else (placed_at, pair, side, intent.amount) — precise
        enough because placed_at has microsecond resolution.

        Conflict policy: on duplicate key, the on-disk file wins.
        After the merge, the next _save_snapshot rewrites the snapshot
        to match.
        """
        def _key(entry):
            t = entry.get("placed_at", "")
            ref = entry.get("order_ref") or {}
            order_id = ref.get("order_id") if isinstance(ref, dict) else None
            if order_id:
                return (t, order_id)
            intent = entry.get("intent") or {}
            return (t, entry.get("pair", ""), entry.get("side", ""),
                    intent.get("amount", 0) if isinstance(intent, dict) else 0)

        seen = {_key(e): e for e in self.order_journal}
        merged_count = 0
        overwritten_count = 0

        # Merge from rolling journal + optional backfill file (manual trades).
        # Backfill file is consumed once and deleted after successful merge.
        rolling_file = os.path.join(self._snapshot_dir, "hydra_order_journal.json")
        backfill_file = os.path.join(self._snapshot_dir, "hydra_order_journal_backfill.json")
        backfill_consumed = False

        for filepath in (rolling_file, backfill_file):
            if not os.path.exists(filepath):
                continue
            try:
                with open(filepath, "r") as f:
                    on_disk = json.load(f)
            except Exception as e:
                print(f"  [JOURNAL] Could not read {os.path.basename(filepath)} for merge: {e}")
                continue
            if not isinstance(on_disk, list):
                continue
            for e in on_disk:
                k = _key(e)
                if k not in seen:
                    seen[k] = e
                    merged_count += 1
                else:
                    # On-disk file wins on conflict — see docstring.
                    if seen[k] is not e:
                        seen[k] = e
                        overwritten_count += 1
            if filepath == backfill_file:
                backfill_consumed = True

        merged = sorted(seen.values(), key=lambda e: e.get("placed_at", ""))
        if len(merged) > self.ORDER_JOURNAL_CAP:
            merged = merged[-self.ORDER_JOURNAL_CAP:]
        self.order_journal = merged
        self._normalize_journal_pairs(self.order_journal)

        if backfill_consumed:
            try:
                os.remove(backfill_file)
                print(f"  [JOURNAL] Consumed and removed backfill file")
            except OSError:
                pass

        if merged_count or overwritten_count:
            parts = []
            if merged_count:
                parts.append(f"merged {merged_count} new")
            if overwritten_count:
                parts.append(f"overwrote {overwritten_count} stale")
            print(f"  [JOURNAL] {' + '.join(parts)}; "
                  f"total = {len(self.order_journal)}")

    def run(self):
        """Main agent loop."""
        self.start_time = time.time()
        self._print_banner()

        # Start WebSocket server for dashboard
        self.broadcaster.start()
        time.sleep(0.5)

        # Set dead man's switch (live mode only) — timeout must exceed tick interval
        self._dms_timeout = max(60, self.interval + 30)
        if not self.paper:
            print(f"  [HYDRA] Setting dead man's switch ({self._dms_timeout}s)...")
            result = KrakenCLI.cancel_after(self._dms_timeout)
            if "error" not in result:
                print("  [HYDRA] Dead man's switch active")
            else:
                print(f"  [WARN] Dead man's switch: {result.get('error', 'unknown')}")

        # Load dynamic pair constants from Kraken (PRICE_DECIMALS, ordermin, costmin).
        # Hardcoded constants remain as fallbacks for any pair not returned.
        if not self.paper:
            print("\n  [HYDRA] Loading pair constants from Kraken...")
            pair_constants = KrakenCLI.load_pair_constants(self.pairs)
            if pair_constants:
                KrakenCLI.apply_pair_constants(pair_constants)
                for pair in self.pairs:
                    self.engines[pair].sizer.apply_pair_limits(pair_constants)
                loaded_pairs = ", ".join(
                    f"{p}(dec={pair_constants[p]['price_decimals']},min={pair_constants[p]['ordermin']})"
                    for p in pair_constants
                )
                print(f"  [HYDRA] Pair constants loaded: {loaded_pairs}")
            else:
                print("  [WARN] Pair constants unavailable — using hardcoded fallbacks")
            time.sleep(2)  # Rate limit

        # Warmup: fetch historical candles for each pair (needed before balance conversion)
        print("\n  [HYDRA] Warming up with historical candles...")
        for pair in self.pairs:
            candles = KrakenCLI.ohlc(pair, interval=self.candle_interval)
            if candles:
                for c in candles[-200:]:
                    self.engines[pair].ingest_candle(c)
                price = candles[-1]["close"]
                print(f"  [HYDRA] {pair}: {min(len(candles), 200)} candles loaded, last price: ${price:,.4f}")
            else:
                print(f"  [WARN] {pair}: no historical data")
            time.sleep(2)  # Respect rate limits

        # Fetch live account balance and initialize engines from real funds
        print("\n  [HYDRA] Checking account balance...")
        bal = KrakenCLI.balance()
        balances_converted = False
        if "error" not in bal:
            for asset, amount in bal.items():
                print(f"  [HYDRA]   {asset}: {amount}")

            # Cache BEFORE _set_engine_balances so v2.11.0's live-path
            # `tradable` flag initialization can read real BTC/quote holdings.
            # Prior ordering marked every non-USD pair info-only at startup
            # until the first tick's _refresh_tradable_flags() self-corrected.
            self._cached_balance = bal

            if not self.paper:
                # Compute tradable USD balance (excludes staked/bonded assets)
                breakdown = self._compute_balance_usd(bal)
                tradable = breakdown["tradable_usd"]
                staked = breakdown["staked_usd"]
                total = breakdown["total_usd"]
                print(f"  [HYDRA] Portfolio: ${total:,.2f} total | ${tradable:,.2f} tradable | ${staked:,.2f} staked")

                if tradable > 0:
                    per_pair_usd = tradable / len(self.pairs)
                    self._set_engine_balances(per_pair_usd)
                    balances_converted = True
                    self.initial_balance = tradable
                    # Lock in competition starting balance on first start only —
                    # on --resume, preserve the original so cumulative P&L is correct.
                    if self._competition_start_balance is None:
                        self._competition_start_balance = tradable
                    print(f"  [HYDRA] Engine balance set from exchange: ${per_pair_usd:,.2f} per pair")
                else:
                    print(f"  [WARN] No tradable balance — using --balance fallback: ${self.initial_balance:,.2f}")
        else:
            print(f"  [WARN] Balance check failed: {bal} — using --balance fallback: ${self.initial_balance:,.2f}")

        # Convert engine balances from USD to quote currency for non-USD pairs
        # (e.g. SOL/BTC engine needs balance in BTC, not USD).
        # Skip if _set_engine_balances was already called above (live mode with
        # exchange data).  Resumed sessions still need conversion because old
        # snapshots (pre-multi-currency fix) stored USD values for BTC-quoted pairs.
        if not balances_converted:
            per_pair_usd = self.initial_balance / len(self.pairs)
            self._set_engine_balances(per_pair_usd)

        # Ensure competition start balance is set (fallback/paper path)
        if self._competition_start_balance is None:
            self._competition_start_balance = self.initial_balance

        # Start the execution stream (kraken ws executions subprocess +
        # background reader). Paper mode no-ops. Failure leaves healthy=False
        # which we surface each tick; placement still works, lifecycle
        # finalization just won't happen until the stream recovers.
        if not self.execution_stream.start():
            print("  [WARN] ExecutionStream failed to start — placements will not auto-finalize")

        # Start push-based market data streams (candle + ticker).
        # Failure is non-fatal — _fetch_and_tick falls back to REST.
        if not self.paper:
            if not self.candle_stream.start():
                print("  [WARN] CandleStream failed to start — falling back to REST ohlc")
            if not self.ticker_stream.start():
                print("  [WARN] TickerStream failed to start — falling back to REST ticker")
            if not self.balance_stream.start():
                print("  [WARN] BalanceStream failed to start — falling back to REST balance")
            if not self.book_stream.start():
                print("  [WARN] BookStream failed to start — falling back to REST depth")

        # Reconcile stale PLACED journal entries from previous sessions.
        # After --resume, the journal may contain entries that finalized on
        # the exchange while we were offline. Query the exchange and update
        # lifecycle state; register still-open orders with the live stream.
        if not self.paper:
            self._reconcile_stale_placed()

        print(f"\n  [HYDRA] Starting LIVE trading loop")
        print(f"  [HYDRA] Pairs: {', '.join(self.pairs)}")
        print(f"  [HYDRA] Interval: {self.interval}s | Duration: {self.duration}s")
        print(f"  {'='*80}")

        tick = 0
        while self.running and (self.duration == 0 or (time.time() - self.start_time) < self.duration):
            # HF-004 fix: wrap the tick body in try/except so an unhandled
            # exception does not kill the run() loop. When start_hydra.bat
            # restarts the agent after a crash, in-memory order_journal
            # entries since the last snapshot are lost. Log the traceback
            # and continue.
            journal_size_start = len(self.order_journal)
            try:
                tick += 1
                elapsed = time.time() - self.start_time
                remaining = "∞" if self.duration == 0 else f"{self.duration - elapsed:.0f}s"

                # v2.13.0: Thesis on_tick is a no-op in Phase A (drift-safe)
                # but Phase C/D extend it to drain the Grok processor queue
                # and expire stale ladder rungs. Hook exists now so the
                # integration point is stable across the phase rollout.
                if self.thesis is not None:
                    try:
                        self.thesis.on_tick(time.time())
                    except Exception as te:
                        print(f"  [THESIS] on_tick error ({type(te).__name__}: {te})")

                ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                print(f"\n  === Tick {tick} | {ts} | Elapsed: {elapsed:.0f}s | Remaining: {remaining} ===")

                # Phase 0: System status gate — skip tick during maintenance.
                # post_only is fine (we only place post-only orders). API failure
                # degrades gracefully to "online" so we never stall on a broken
                # status endpoint.
                if not self.paper:
                    _status_resp = KrakenCLI.system_status()
                    _kraken_status = (
                        _status_resp.get("status", "online")
                        if isinstance(_status_resp, dict) and "error" not in _status_resp
                        else "online"
                    )
                    if _kraken_status not in ("online", "post_only"):
                        if self._last_kraken_status != _kraken_status:
                            print(f"  [HYDRA] Kraken status: {_kraken_status} — skipping tick")
                        self._last_kraken_status = _kraken_status
                        # Sleep the full tick interval to avoid busy-looping
                        next_tick_time = self.start_time + tick * self.interval
                        _maint_sleep = next_tick_time - time.time()
                        if _maint_sleep > 0 and self.running:
                            time.sleep(_maint_sleep)
                        continue
                    if self._last_kraken_status not in ("online", "post_only", None):
                        print(f"  [HYDRA] Kraken back online (was {self._last_kraken_status})")
                    self._last_kraken_status = _kraken_status
                    time.sleep(2)  # Rate limit

                # Refresh dead man's switch every tick (live mode only)
                if not self.paper:
                    KrakenCLI.cancel_after(self._dms_timeout)
                    time.sleep(2)  # Rate limit

                # Phase 0.5: Re-evaluate per-engine `tradable` flags from the
                # latest balance snapshot. Flips an engine to informational-
                # only when its quote currency is depleted, or re-activates
                # it when the operator (or a BTC/USDC fill) tops it back up.
                # Cheap dict lookup; transition logging only, no tick spam.
                if not self.paper:
                    self._refresh_tradable_flags()

                # Phase 1: Fetch data and run all engines (regimes, signals, positions)
                engine_states = {}
                for pair in self.pairs:
                    engine_states[pair] = self._fetch_and_tick(pair)

                # Capture engine's original signal before any external modifiers
                original_signals = {}
                for pair, state in engine_states.items():
                    if state:
                        original_signals[pair] = {
                            "action": state["signal"]["action"],
                            "confidence": state["signal"]["confidence"],
                        }

                # Phase 1.5: Cross-pair regime coordination
                # Update coordinator with latest regimes, then apply overrides
                for pair, state in engine_states.items():
                    if state:
                        self.coordinator.update(pair, state.get("regime", "RANGING"))

                # Rule 4 confluence needs price histories — pull from the
                # engines rather than bloating the broadcast state dicts.
                price_series = {
                    p: list(self.engines[p].prices) for p in self.pairs
                }
                cross_overrides = self.coordinator.get_overrides(
                    engine_states, price_series=price_series,
                )
                pending_swaps = []
                for pair, override in cross_overrides.items():
                    state = engine_states.get(pair)
                    if not state:
                        continue
                    print(f"  [CROSS] {pair}: {override['action']} → {override['signal']} "
                          f"(conf {override['confidence_adj']:.2f}) — {override['reason']}")
                    state["signal"]["action"] = override["signal"]
                    state["signal"]["confidence"] = override["confidence_adj"]
                    state["signal"]["reason"] = f"[CROSS-PAIR] {override['reason']}"
                    state["cross_pair_override"] = override
                    # Collect swap opportunities for execution after trades
                    if override.get("swap"):
                        pending_swaps.append(override["swap"])

                # If coordinator changed signal direction, reset baseline for cap
                for pair in self.pairs:
                    orig = original_signals.get(pair)
                    state = engine_states.get(pair)
                    if orig and state and state["signal"]["action"] != orig["action"]:
                        original_signals[pair]["confidence"] = state["signal"]["confidence"]

                # Phase 1.75: Order book intelligence
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state:
                        continue
                    depth = self.book_stream.latest_book(pair)
                    if isinstance(depth, dict) and "error" not in depth:
                        signal_action = state["signal"].get("action", "HOLD")
                        book_analysis = OrderBookAnalyzer.analyze(depth, signal_action)
                        state["order_book"] = book_analysis
                        # Apply modifier to signal confidence
                        old_conf = state["signal"]["confidence"]
                        new_conf = max(0.0, min(1.0, old_conf + book_analysis["confidence_modifier"]))
                        if book_analysis["confidence_modifier"] != 0:
                            state["signal"]["confidence"] = new_conf
                            print(f"  [BOOK] {pair}: imbalance {book_analysis['imbalance_ratio']:.2f}, "
                                  f"spread {book_analysis['spread_bps']:.1f}bps, "
                                  f"conf {old_conf:.2f} → {new_conf:.2f} "
                                  f"(mod {book_analysis['confidence_modifier']:+.2f})"
                                  f"{' [BID WALL]' if book_analysis['bid_wall'] else ''}"
                                  f"{' [ASK WALL]' if book_analysis['ask_wall'] else ''}")

                # Phase 1.8: FOREX session-aware confidence weighting
                # Crypto volume clusters around traditional FX sessions.
                # London/NY overlap (12-16 UTC) is peak liquidity → signals more reliable.
                # Dead zone (21-00 UTC) is thinnest → signals less reliable.
                utc_hour = datetime.now(timezone.utc).hour
                if 12 <= utc_hour < 16:      # London/NY overlap — peak
                    session_mod = 0.04
                    session_label = "London/NY"
                elif 7 <= utc_hour < 12:      # London session
                    session_mod = 0.02
                    session_label = "London"
                elif 16 <= utc_hour < 21:     # NY session
                    session_mod = 0.02
                    session_label = "New York"
                elif 0 <= utc_hour < 7:       # Asian session
                    session_mod = -0.03
                    session_label = "Asian"
                else:                          # 21-00 UTC dead zone
                    session_mod = -0.05
                    session_label = "dead zone"

                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state or session_mod == 0:
                        continue
                    old_conf = state["signal"]["confidence"]
                    new_conf = max(0.0, min(1.0, old_conf + session_mod))
                    if old_conf != new_conf and state["signal"]["action"] != "HOLD":
                        state["signal"]["confidence"] = new_conf
                        if abs(session_mod) >= 0.03:  # Only log notable adjustments
                            print(f"  [SESSION] {pair}: {session_label} ({utc_hour:02d}:xx UTC), "
                                  f"conf {old_conf:.2f} → {new_conf:.2f} ({session_mod:+.2f})")

                # ── Total modifier cap ──────────────────────────────────
                # External modifiers (cross-pair + order book + session) can reduce confidence
                # without limit but cannot boost it more than +0.15 above the engine's original
                # signal.  This prevents stacking modifiers from inflating weak signals into
                # high-conviction trades that get oversized via Kelly criterion.
                MAX_TOTAL_MODIFIER_BOOST = 0.15
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    orig = original_signals.get(pair)
                    if not state or not orig:
                        continue
                    orig_conf = orig["confidence"]
                    if state["signal"]["confidence"] > orig_conf + MAX_TOTAL_MODIFIER_BOOST:
                        state["signal"]["confidence"] = orig_conf + MAX_TOTAL_MODIFIER_BOOST
                    if state["signal"]["confidence"] < 0.0:
                        state["signal"]["confidence"] = 0.0

                # Phase 1.9: Compute aggregate portfolio context for brain
                try:
                    self._current_portfolio_summary = self._build_portfolio_summary()
                except Exception:
                    self._current_portfolio_summary = {}

                # Phase 1.95: Periodic portfolio strategist review (Grok)
                # Track candle epoch — advances when ALL pairs have new timestamps
                epoch_advanced = True
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state:
                        epoch_advanced = False
                        break
                    candles = state.get("candles", [])
                    ts = candles[-1]["t"] if candles else 0
                    prev = self._portfolio_candle_epoch.get(pair, 0.0)
                    if ts <= prev:
                        epoch_advanced = False
                        break
                if epoch_advanced:
                    for pair in self.pairs:
                        state = engine_states.get(pair)
                        candles = state.get("candles", []) if state else []
                        self._portfolio_candle_epoch[pair] = candles[-1]["t"] if candles else 0
                    self._portfolio_epoch_count += 1

                # Check for multi-pair regime transitions (2+ pairs changed)
                regime_changes = 0
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if state:
                        current = state.get("regime", "RANGING")
                        if self._last_portfolio_review_regimes.get(pair) != current:
                            regime_changes += 1
                force_portfolio_review = regime_changes >= 2

                should_review = (
                    (self._portfolio_epoch_count >= 3 or force_portfolio_review)
                    and self.brain and self.brain.has_strategist
                )
                if should_review:
                    review_state = self._build_portfolio_review_state(engine_states)
                    guidance = self.brain.run_portfolio_review(review_state)
                    if guidance:
                        self._portfolio_guidance = guidance
                        print(f"  [PORTFOLIO] New guidance: {guidance[:100]}...")
                    self._portfolio_epoch_count = 0
                    self._last_portfolio_review_regimes = {
                        p: (engine_states.get(p) or {}).get("regime", "RANGING")
                        for p in self.pairs
                    }

                # Phase 2: Run brain with full cross-pair context (parallel across pairs)
                all_states = {}
                brain_pairs = []
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if state:
                        if state["signal"]["action"] != "HOLD" and self.brain:
                            brain_pairs.append((pair, state))
                        else:
                            # Inject cached brain decision for dashboard persistence
                            cached = self._last_ai_decision.get(pair)
                            if cached and self.brain:
                                state["ai_decision"] = cached
                            all_states[pair] = state

                if brain_pairs:
                    with ThreadPoolExecutor(max_workers=len(brain_pairs)) as executor:
                        futures = {
                            executor.submit(self._apply_brain, pair, state, engine_states): pair
                            for pair, state in brain_pairs
                        }
                        for future in as_completed(futures):
                            pair = futures[future]
                            try:
                                all_states[pair] = future.result(timeout=60)
                            except Exception as e:
                                print(f"  [WARN] Brain failed for {pair}: {e}")
                                all_states[pair] = engine_states[pair]

                # Phase 2.5: Execute finalized signals on engines (deferred from generate_only)
                # When brain is active, tick() ran with generate_only=True, so we must
                # now execute the final (possibly brain-modified) signals on the engines.
                # Skip pairs involved in pending swaps — the swap handler manages their execution.
                swap_pairs = set()
                if pending_swaps:
                    for s in pending_swaps:
                        swap_pairs.add(s["sell_pair"])
                        swap_pairs.add(s["buy_pair"])
                if self.brain:
                    for pair in self.pairs:
                        if pair in swap_pairs:
                            continue
                        state = all_states.get(pair)
                        if not state:
                            continue
                        sig = state.get("signal", {})
                        ai = state.get("ai_decision", {})
                        engine = self.engines[pair]
                        pre_trade_snap = engine.snapshot_position()
                        # v2.13.1 (Phase B): compose brain's size_multiplier
                        # with thesis size_hint. In default advisory mode,
                        # size_hint is 1.0 so composition is a no-op and
                        # Phase A behavior is preserved. Only binding
                        # enforcement (Phase E, opt-in) moves size_hint off
                        # 1.0. Final product is clamped to [0.0, 1.5] so
                        # no stacked modifiers can exceed Kelly's hard cap.
                        thesis_attr = getattr(self, "thesis", None)
                        _size_hint = 1.0
                        if thesis_attr is not None and not thesis_attr.disabled:
                            try:
                                _size_hint = thesis_attr.size_hint_for(pair, sig)
                            except Exception as te:
                                print(f"  [THESIS] size_hint_for error ({type(te).__name__}: {te})")
                        _brain_mult = float(ai.get("size_multiplier", 1.0) or 1.0)
                        _final_mult = max(0.0, min(1.5, _brain_mult * _size_hint))
                        trade = engine.execute_signal(
                            action=sig.get("action", "HOLD"),
                            confidence=sig.get("confidence", 0),
                            reason=sig.get("reason", ""),
                            strategy=state.get("strategy", "MOMENTUM"),
                            size_multiplier=_final_mult,
                        )
                        if trade is None and sig.get("action") in ("BUY", "SELL") and ai:
                            print(f"  [BRAIN] {pair}: {sig['action']} signal did not execute "
                                  f"(conf={sig.get('confidence', 0):.2f}, "
                                  f"size_mult={ai.get('size_multiplier', 1.0):.2f}, "
                                  f"brain={ai.get('action', '?')})")
                        if trade:
                            is_usd_pair = pair.endswith("USDC") or pair.endswith("USD")
                            value_decimals = 2 if is_usd_pair else 8
                            state["last_trade"] = {
                                "action": trade.action,
                                "price": round(trade.price, 8),
                                "amount": round(trade.amount, 8),
                                "value": round(trade.value, value_decimals),
                                "reason": trade.reason,
                                "confidence": round(trade.confidence, 4),
                                "profit": round(trade.profit, value_decimals) if trade.profit is not None else None,
                                "params_at_entry": trade.params_at_entry,
                            }
                            state["_pre_trade_snapshot"] = pre_trade_snap

                # Print status and place orders (sequential — rate limiting required)
                # Skip swap pairs — the swap handler manages their execution.
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if state:
                        self._print_tick_status(pair, state)
                        if state.get("last_trade") and pair not in swap_pairs:
                            success = self._place_order(pair, state["last_trade"], state)
                            if not success and state.get("_pre_trade_snapshot"):
                                engine = self.engines[pair]
                                engine.restore_position(state["_pre_trade_snapshot"])
                                print(f"  [ROLLBACK] {pair}: engine state rolled back after failed placement")

                # Phase 3: Execute coordinated swaps, then check regime transitions
                if pending_swaps:
                    for swap in pending_swaps:
                        self._execute_coordinated_swap(swap, all_states)
                self._log_regime_transitions(all_states)

                # Phase 4: Record trade outcomes for self-tuning
                # Only record when a position is fully closed so the tuner learns
                # from the total accumulated P&L, not individual partial-sell legs.
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if not state or not state.get("last_trade"):
                        continue
                    trade = state["last_trade"]
                    engine = self.engines[pair]
                    if trade["action"] == "SELL" and trade.get("profit") is not None and engine.position.size == 0:
                        params_at_entry = trade.get("params_at_entry") or engine.snapshot_params()
                        outcome = "win" if trade["profit"] > 0 else "loss"
                        self.trackers[pair].record_trade(
                            params_at_entry, "SELL", outcome, trade["profit"],
                        )
                        self._completed_trades_since_update += 1

                # Run tuner updates every 50 completed trades
                if self._completed_trades_since_update >= 50:
                    self._run_tuner_update()

                # Strip internal rollback data before broadcasting to dashboard
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if state:
                        state.pop("_pre_trade_snapshot", None)

                # Refresh performance/portfolio/position in state dicts from
                # engine's actual state. When brain is active, tick() ran with
                # generate_only=True so the state dict was built BEFORE
                # execute_signal() updated counters.  Even without brain,
                # a failed order + rollback can desync the dict.  Refreshing
                # here ensures the dashboard always sees authoritative values.
                for pair in self.pairs:
                    state = all_states.get(pair)
                    if not state:
                        continue
                    engine = self.engines[pair]
                    current_price = engine.prices[-1] if engine.prices else 0
                    equity = engine.balance + (engine.position.size * current_price)
                    is_usd_pair = pair.endswith("USDC") or pair.endswith("USD")
                    vd = 2 if is_usd_pair else 8
                    pnl_pct = ((equity - engine.initial_balance) / engine.initial_balance * 100) if engine.initial_balance > 0 else 0
                    wl = engine.win_count + engine.loss_count
                    win_rate = (engine.win_count / wl * 100) if wl > 0 else 0
                    state["performance"] = {
                        "total_trades": engine.total_trades,
                        "win_count": engine.win_count,
                        "loss_count": engine.loss_count,
                        "win_rate_pct": round(win_rate, 2),
                        "sharpe_estimate": round(engine._calc_sharpe(), 4),
                    }
                    state["portfolio"] = {
                        "balance": round(engine.balance, vd),
                        "equity": round(equity, vd),
                        "pnl_pct": round(pnl_pct, 4),
                        "max_drawdown_pct": round(engine.max_drawdown, 4),
                        "peak_equity": round(engine.peak_equity, vd),
                    }
                    state["position"] = {
                        "size": round(engine.position.size, 8),
                        "avg_entry": round(engine.position.avg_entry, 8),
                        "unrealized_pnl": round(engine.position.unrealized_pnl, vd),
                    }

                # Broadcast state to dashboard (uses cached balance, no extra API call)
                dashboard_state = self._build_dashboard_state(tick, all_states, elapsed)
                self.broadcaster.broadcast(dashboard_state)

                # Drain queued WS execution events and apply them to the
                # journal + engine state. Pushes, not polls — the stream
                # has been delivering events in the background since tick
                # start. In paper mode this drains any synthetic fills
                # _place_paper_order injected during this tick.
                #
                # Health policy: ensure_healthy() reports current state and,
                # in live mode, attempts an auto-restart of the subprocess
                # if it's dead (subject to RESTART_COOLDOWN_S). The warning
                # is rate-limited to transitions — printing every tick spams
                # the operator and obscures the actionable signal. The reason
                # string identifies WHICH check failed so we can debug.
                if not self.execution_stream.paper:
                    healthy, reason = self.execution_stream.ensure_healthy()
                    if not healthy:
                        if self._exec_stream_warned_reason != reason:
                            print(
                                f"  [WARN] execution stream unhealthy — {reason} "
                                f"(lifecycle finalization stalled)"
                            )
                            self._exec_stream_warned_reason = reason
                    elif self._exec_stream_warned_reason is not None:
                        print("  [EXECSTREAM] stream healthy again")
                        self._exec_stream_warned_reason = None
                for term in self.execution_stream.drain_events():
                    self._apply_execution_event(term)

                # Market data stream health — auto-restart if dead.
                # No transition logging needed; REST fallback is seamless.
                if not self.paper:
                    self.candle_stream.ensure_healthy()
                    self.ticker_stream.ensure_healthy()
                    self.balance_stream.ensure_healthy()
                    self.book_stream.ensure_healthy()

                # Rolling save — persist the order journal every tick so
                # no data is lost on crash. Atomic write (.tmp + os.replace)
                # so a crash mid-write cannot corrupt the file into
                # half-valid JSON. Mirrors _save_snapshot's pattern.
                if self.order_journal:
                    rolling_file = os.path.join(self._snapshot_dir, "hydra_order_journal.json")
                    rolling_tmp = rolling_file + ".tmp"
                    try:
                        with open(rolling_tmp, "w") as f:
                            json.dump(self.order_journal, f, indent=2)
                        os.replace(rolling_tmp, rolling_file)
                    except Exception as e:
                        # HF-003 fix: previously "except Exception: pass" silently
                        # swallowed write failures (permission, disk, lock, etc.),
                        # making logging outages invisible. Log the failure so it's
                        # visible in stdout and in hydra_errors.log via the outer
                        # tick-body exception handler.
                        print(f"  [WARN] rolling journal write failed: {type(e).__name__}: {e}")

                # Cap order journal to prevent unbounded memory growth
                if len(self.order_journal) > self.ORDER_JOURNAL_CAP:
                    self.order_journal = self.order_journal[-self.ORDER_JOURNAL_CAP:]


            except Exception as e:
                print(f"  [ERROR] Tick {tick} crashed: {type(e).__name__}: {e}")
                try:
                    err_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hydra_errors.log")
                    with open(err_file, "a", encoding="utf-8") as f:
                        f.write(f"\n=== Tick {tick} @ {datetime.now(timezone.utc).isoformat()} ===\n")
                        f.write(traceback.format_exc())
                except Exception:
                    pass  # if error log write fails, at least we printed to stdout

            # HF-004 fix: snapshot immediately if the journal grew this tick,
            # so a subsequent crash does not lose the newly-appended entries.
            # Also save on the periodic cadence for engine state that
            # changes without placements.
            journal_grew = len(self.order_journal) > journal_size_start
            if journal_grew or tick % self.SNAPSHOT_EVERY_N_TICKS == 0:
                self._save_snapshot()

            # Sleep until next tick
            next_tick_time = self.start_time + tick * self.interval
            sleep_time = next_tick_time - time.time()
            if sleep_time > 0 and self.running:
                time.sleep(sleep_time)
        # Final tuner update on shutdown
        self._run_tuner_update()

        # Final report
        self._print_final_report()

    def _fetch_and_tick(self, pair: str) -> Optional[dict]:
        """Phase 1: Fetch latest data from Kraken and run engine tick.

        Prefers WS candle stream when healthy (zero API calls, zero sleep).
        Falls back to REST ohlc() → REST ticker() when stream is unavailable.

        When a brain is active, uses generate_only=True so the engine produces
        signals without executing trades internally. This prevents engine state
        from diverging when the brain later overrides a signal.
        """
        engine = self.engines[pair]
        candle_ingested = False

        # Try WS candle stream first (no API call, no rate-limit sleep)
        ws_candle = (
            self.candle_stream.latest_candle(pair)
            if self.candle_stream.healthy
            else None
        )
        if ws_candle:
            # Convert WS ohlc shape to engine candle format.
            # WS uses interval_begin (ISO) or timestamp; parse to epoch.
            ts_raw = ws_candle.get("interval_begin") or ws_candle.get("timestamp")
            if isinstance(ts_raw, str):
                try:
                    ts = datetime.fromisoformat(ts_raw.replace("Z", "+00:00")).timestamp()
                except Exception:
                    ts = time.time()
            elif isinstance(ts_raw, (int, float)):
                ts = float(ts_raw)
            else:
                ts = time.time()
            engine.ingest_candle({
                "open": ws_candle.get("open", 0),
                "high": ws_candle.get("high", 0),
                "low": ws_candle.get("low", 0),
                "close": ws_candle.get("close", 0),
                "volume": ws_candle.get("volume", 0),
                "timestamp": ts,
            })
            candle_ingested = True

        if not candle_ingested:
            # CandleStream unavailable — skip tick for this pair.
            # Engine retains previous candle data from warmup / prior ticks.
            return None

        # Snapshot position before tick so we can rollback if exchange order fails.
        # When generate_only=True (brain active), execute_signal happens later and
        # snapshots there. When generate_only=False, tick() may execute internally.
        pre_trade_snap = engine.snapshot_position() if not self.brain else None
        state = engine.tick(generate_only=bool(self.brain))
        if pre_trade_snap and state.get("last_trade"):
            state["_pre_trade_snapshot"] = pre_trade_snap
        return state

    def _apply_brain(self, pair: str, state: dict, all_engine_states: dict) -> dict:
        """Phase 2: Run brain with full cross-pair context. Mutates state in place."""
        if not self.brain or state["signal"]["action"] == "HOLD":
            # Inject cached decision for dashboard persistence (brain didn't fire)
            cached = self._last_ai_decision.get(pair)
            if cached:
                state["ai_decision"] = cached
            return state

        # Pre-brain filter: skip brain for BUY signals that can't produce tradeable order size
        if state["signal"]["action"] == "BUY":
            engine = self.engines[pair]
            test_size = engine.sizer.calculate(
                state["signal"]["confidence"], engine.balance, state["price"], pair,
            )
            if test_size == 0:
                cached = self._last_ai_decision.get(pair)
                if cached:
                    state["ai_decision"] = cached
                return state  # Signal too weak to trade; don't waste brain tokens

        # Candle-freshness gate: only invoke brain when the pair has a NEW candle.
        # On forming-candle updates (same interval_begin), the engine deduplicates
        # in place — indicators are near-identical.  Skip brain to avoid duplicate
        # evaluation on unchanged data.
        candles = state.get("candles", [])
        current_candle_ts = candles[-1]["t"] if candles else 0.0
        last_ts = self._last_brain_candle_ts.get(pair, 0.0)
        if current_candle_ts > 0 and current_candle_ts == last_ts:
            cached = self._last_ai_decision.get(pair)
            if cached:
                state["ai_decision"] = cached
            return state  # Same candle as last brain evaluation — skip

        # Inject cross-pair triangle context and portfolio-level awareness
        state["triangle_context"] = self._build_triangle_context(pair, all_engine_states)
        state["portfolio_summary"] = self._current_portfolio_summary
        if self._portfolio_guidance:
            state["portfolio_guidance"] = self._portfolio_guidance

        # v2.13.1 (Phase B): inject ThesisContext so the analyst can reason
        # with the persistent thesis layer. Absent → empty string block in
        # the prompt, matching v2.12.5 output byte-for-byte.
        thesis_attr = getattr(self, "thesis", None)
        if thesis_attr is not None and not thesis_attr.disabled:
            try:
                state["thesis_context"] = thesis_attr.context_for(pair, state.get("signal"))
            except Exception as te:
                print(f"  [THESIS] context_for error ({type(te).__name__}: {te})")
                state["thesis_context"] = None

        # Fetch spread data for risk assessment. Prefer WS ticker (no API call).
        try:
            ticker = self.ticker_stream.latest_ticker(pair) or {}
            if "error" not in ticker and "bid" in ticker:
                bid, ask = ticker["bid"], ticker["ask"]
                mid = (bid + ask) / 2
                spread_bps = round((ask - bid) / mid * 10000, 1) if mid > 0 else 0
                state["spread"] = {"bid": bid, "ask": ask, "spread_bps": spread_bps}
        except Exception:
            pass

        try:
            decision = self.brain.deliberate(state)
            state["ai_decision"] = {
                "action": decision.action,
                "final_signal": decision.final_signal,
                "confidence_adj": decision.confidence_adj,
                "size_multiplier": decision.size_multiplier,
                "analyst_reasoning": decision.analyst_reasoning,
                "risk_reasoning": decision.risk_reasoning,
                "strategist_reasoning": decision.strategist_reasoning,
                "escalated": decision.escalated,
                "summary": decision.combined_summary,
                "risk_flags": decision.risk_flags,
                "portfolio_health": decision.portfolio_health,
                "fallback": decision.fallback,
                "tokens_used": decision.tokens_used,
                "latency_ms": round(decision.latency_ms, 0),
                # v2.13.1: thesis alignment (None when thesis absent).
                "thesis_alignment": decision.thesis_alignment,
            }
            # Cache for dashboard persistence on ticks where brain doesn't fire
            self._last_ai_decision[pair] = state["ai_decision"]

            # Mark candle as evaluated only when brain ran LLM calls (not fallback).
            # On fallback (budget exceeded, API down), leave timestamp unchanged so
            # the next tick retries this candle.
            if not decision.fallback:
                self._last_brain_candle_ts[pair] = current_candle_ts

            # Apply AI decision to engine state
            # Note: engine ran with generate_only=True, so no trade was executed yet.
            # Brain controls sizing via size_multiplier only — engine confidence
            # passes through untouched to Kelly criterion.  confidence_adj is
            # preserved in state["ai_decision"] for dashboard/logging.
            if decision.action == "OVERRIDE":
                state["signal"]["action"] = decision.final_signal
                state["signal"]["reason"] = f"[AI OVERRIDE] {decision.combined_summary}"
            elif decision.action == "ADJUST":
                state["signal"]["reason"] = f"[AI ADJUSTED] {decision.combined_summary}"
            # CONFIRM leaves signal unchanged, just adds reasoning
        except Exception as e:
            state["ai_decision"] = {"action": "FALLBACK", "error": str(e), "fallback": True}
            # Do NOT update _last_brain_candle_ts — allow retry on next tick

        return state

    def _build_triangle_context(self, current_pair: str, all_states: dict) -> dict:
        """Build cross-pair context summary for brain deliberation."""
        pairs = {}
        sol_exposure = 0.0
        btc_exposure = 0.0

        for pair, state in all_states.items():
            if state is None:
                continue
            pos = state.get("position", {}).get("size", 0)
            price = state.get("price", 0)

            # Net asset exposure across the triangle.
            # Spot positions: holding SOL (whether purchased via USDC or BTC)
            # only adds SOL exposure. The BTC spent on a SOL/BTC buy is
            # already reflected in the account's BTC balance, not a
            # synthetic "short BTC" obligation — this is spot trading, not
            # margin. BTC exposure comes exclusively from BTC/USDC holdings.
            if pair == "SOL/USDC":
                sol_exposure += pos
            elif pair == "SOL/BTC":
                sol_exposure += pos
            elif pair == "BTC/USDC":
                btc_exposure += pos

            # Sibling pair summaries (exclude current pair)
            if pair != current_pair:
                pairs[pair] = {
                    "regime": state.get("regime", "UNKNOWN"),
                    "signal": state.get("signal", {}).get("action", "HOLD"),
                    "confidence": state.get("signal", {}).get("confidence", 0),
                    "position_size": pos,
                    "price": price,
                }

        return {
            "pairs": pairs,
            "net_exposure": {
                "SOL": round(sol_exposure, 6),
                "BTC": round(btc_exposure, 6),
            },
        }

    # ─── Portfolio-level awareness ───

    def _build_portfolio_summary(self) -> dict:
        """Compute aggregate portfolio stats across all pairs for brain context."""
        asset_prices = self._get_asset_prices()
        total_equity_usd = 0.0
        total_realized_usd = 0.0
        total_unrealized_usd = 0.0
        total_initial_usd = 0.0
        agg_wins = 0
        agg_losses = 0
        agg_trades = 0
        worst_dd = 0.0
        per_pair_pnl: Dict[str, float] = {}

        for pair in self.pairs:
            engine = self.engines.get(pair)
            if not engine:
                continue
            price = engine.prices[-1] if engine.prices else 0
            equity = engine.balance + (engine.position.size * price)
            quote = pair.split("/")[1] if "/" in pair else "USD"
            quote_usd = asset_prices.get(quote, 1.0)

            # P&L
            realized = self._compute_pair_realized_pnl(pair)
            unrealized = (engine.position.size * (price - engine.position.avg_entry)
                          if engine.position.size > 0 else 0)
            total_realized_usd += realized * quote_usd
            total_unrealized_usd += unrealized * quote_usd
            total_equity_usd += equity * quote_usd
            total_initial_usd += engine.initial_balance * quote_usd
            per_pair_pnl[pair] = round((realized + unrealized) * quote_usd, 2)

            # Aggregate performance
            agg_wins += engine.win_count
            agg_losses += engine.loss_count
            agg_trades += engine.total_trades
            if engine.max_drawdown > worst_dd:
                worst_dd = engine.max_drawdown

        total_pnl_usd = total_realized_usd + total_unrealized_usd
        total_pnl_pct = (total_pnl_usd / total_initial_usd * 100) if total_initial_usd > 0 else 0
        agg_wl = agg_wins + agg_losses
        agg_win_rate = (agg_wins / agg_wl * 100) if agg_wl > 0 else 0

        # Net USD exposure
        net_exposure_usd = 0.0
        for pair in self.pairs:
            engine = self.engines.get(pair)
            if engine and engine.position.size > 0:
                price = engine.prices[-1] if engine.prices else 0
                quote = pair.split("/")[1] if "/" in pair else "USD"
                quote_usd = asset_prices.get(quote, 1.0)
                net_exposure_usd += engine.position.size * price * quote_usd

        # Recent trades from journal (last 10 filled, all pairs)
        FILL_STATES = ("FILLED", "PARTIALLY_FILLED")
        recent_trades = []
        for entry in reversed(self.order_journal):
            lc = entry.get("lifecycle") or {}
            if lc.get("state") not in FILL_STATES:
                continue
            recent_trades.append({
                "pair": entry.get("pair", "?"),
                "side": entry.get("side", "?"),
                "price": lc.get("avg_fill_price") or (entry.get("intent") or {}).get("limit_price") or 0,
                "vol": lc.get("vol_exec") or 0,
                "time": (entry.get("placed_at") or "")[:16],
            })
            if len(recent_trades) >= 10:
                break
        recent_trades.reverse()  # chronological order

        return {
            "total_equity_usd": round(total_equity_usd, 2),
            "total_pnl_usd": round(total_pnl_usd, 2),
            "total_pnl_pct": round(total_pnl_pct, 2),
            "agg_win_rate_pct": round(agg_win_rate, 1),
            "agg_trades": agg_trades,
            "worst_drawdown_pct": round(worst_dd, 2),
            "per_pair_pnl_usd": per_pair_pnl,
            "net_exposure_usd": round(net_exposure_usd, 2),
            "recent_trades": recent_trades,
        }

    def _build_portfolio_review_state(self, engine_states: dict) -> dict:
        """Build enriched portfolio state for Grok portfolio review."""
        ps = dict(self._current_portfolio_summary)
        pair_details = []
        for pair in self.pairs:
            engine = self.engines.get(pair)
            state = engine_states.get(pair, {})
            if not engine:
                continue
            pair_details.append({
                "pair": pair,
                "regime": state.get("regime", "UNKNOWN"),
                "signal": state.get("signal", {}).get("action", "HOLD"),
                "confidence": state.get("signal", {}).get("confidence", 0),
                "position": engine.position.size,
                "pnl_usd": ps.get("per_pair_pnl_usd", {}).get(pair, 0),
                "drawdown": engine.max_drawdown,
                "wins": engine.win_count,
                "losses": engine.loss_count,
            })
        ps["pair_details"] = pair_details
        return ps

    # ─── Order placement (writes the journal, registers with the stream) ───

    # Safety gap: when reseeding from journal history, jump this far ahead
    # so we're not sharing the immediate neighborhood with any recent entry.
    _USERREF_SAFETY_GAP = 1000

    def _journal_max_userref(self) -> int:
        """Scan self.order_journal for the highest integer userref seen.
        Returns 0 if none found."""
        hi = 0
        for entry in self.order_journal:
            if not isinstance(entry, dict):
                continue
            ref = entry.get("order_ref") or {}
            if not isinstance(ref, dict):
                continue
            uref = ref.get("order_userref")
            if isinstance(uref, int) and 0 < uref < (1 << 31) and uref > hi:
                hi = uref
        return hi

    def _reseed_userref_from_history(self) -> None:
        """Raise _userref_counter above anything historically used.

        Called once in __init__ after snapshot load + journal merge. Protects
        against restart-collision: if the previous session left open orders
        with userrefs near the current time (the default seed), a fresh seed
        could re-issue the same userref and route WS fills to the wrong
        journal entry via _userref_to_order_id.
        """
        journal_max = self._journal_max_userref()
        if journal_max > 0:
            new_floor = min(journal_max + self._USERREF_SAFETY_GAP, 0x7FFFFFFF)
            if new_floor > self._userref_counter:
                self._userref_counter = new_floor

    def _next_userref(self) -> int:
        """Monotonic client tag used for --userref on placement so WS
        executions can correlate back to the local journal entry."""
        self._userref_counter += 1
        # Kraken userref is int32. Wrap defensively — re-consult history so
        # the wrap-reseed can't land back on a still-open order's userref.
        if self._userref_counter > 0x7FFFFFFF:
            time_seed = int(time.time()) & 0x7FFFFFFF
            journal_max = self._journal_max_userref()
            self._userref_counter = max(time_seed, journal_max + self._USERREF_SAFETY_GAP)
            if self._userref_counter > 0x7FFFFFFF:
                # Extreme degenerate case: journal has values near 2^31. Fall
                # back to time_seed alone, accepting the micro-collision risk.
                self._userref_counter = time_seed
        return self._userref_counter

    def _build_journal_entry(self, pair: str, trade: dict, state: dict) -> Dict[str, Any]:
        """Construct a new-shape order journal entry from a tick's trade
        intent + decision context. Lifecycle is filled in by the caller
        once placement completes (initial state = PLACED on success,
        PLACEMENT_FAILED on any pre-exchange failure).

        Decision context is pulled from `state` — this is the bot's
        private view of why it's placing the order, and the one thing
        Kraken cannot reconstruct.
        """
        action_upper = trade["action"].upper()
        confidence = trade.get("confidence")
        # Brain verdict summary if the brain fired this tick
        ai = state.get("ai_decision") if isinstance(state, dict) else None
        brain_verdict = None
        if isinstance(ai, dict) and not ai.get("fallback"):
            brain_verdict = {
                "action": ai.get("action"),
                "final_signal": ai.get("final_signal"),
                "summary": ai.get("summary"),
            }
        book = state.get("order_book") if isinstance(state, dict) else None
        book_mod = book.get("confidence_modifier") if isinstance(book, dict) else None
        return {
            "placed_at": datetime.now(timezone.utc).isoformat(),
            "pair": pair,
            "side": action_upper,
            "intent": {
                "amount": trade["amount"],
                "limit_price": trade.get("price"),
                "post_only": not self.paper,
                "order_type": "market" if self.paper else "limit",
                "paper": self.paper,
            },
            "decision": {
                "strategy": state.get("strategy") if isinstance(state, dict) else None,
                "regime": state.get("regime") if isinstance(state, dict) else None,
                "reason": trade.get("reason"),
                "confidence": float(confidence) if isinstance(confidence, (int, float)) else None,
                "params_at_entry": trade.get("params_at_entry"),
                "cross_pair_override": state.get("cross_pair_override") if isinstance(state, dict) else None,
                # Lifted from cross_pair_override when a Rule 4 confluence
                # boost drove the trade. Surfaces {source_pair, rho, bonus,
                # other_conf, window} at the top level for dashboard/analytics
                # consumers that don't want to unwrap the override dict.
                "confluence_source": (
                    (state.get("cross_pair_override") or {}).get("confluence_source")
                    if isinstance(state, dict) else None
                ),
                "book_confidence_modifier": book_mod,
                "brain_verdict": brain_verdict,
                "swap_id": trade.get("swap_id"),
                # v2.13.1 (Phase B): stamp thesis posture at decision time +
                # list of intent-prompt IDs that the analyst consulted. None
                # when thesis is disabled/absent — matching v2.12.5 shape.
                "thesis_posture": self._journal_thesis_posture(),
                "thesis_intents_active": self._journal_intents_active(ai),
                "thesis_alignment": (ai or {}).get("thesis_alignment") if isinstance(ai, dict) else None,
                # v2.13.3 (Phase D) — ladder alignment. Set when the placed
                # (pair, side, price) matches a pending rung of an active
                # ladder. Otherwise "adhoc=true" — still a legal trade, just
                # flagged so the tape distinguishes planned from reactive.
                # Both fields stay None when HYDRA_THESIS_LADDERS is unset
                # so journal schema is stable for users who haven't opted in.
                **self._journal_ladder_stamp(pair, action_upper, trade.get("price")),
            },
            "order_ref": {"order_userref": None, "order_id": None},
            "lifecycle": {
                "state": "PLACED",
                "vol_exec": 0.0,
                "avg_fill_price": None,
                "fee_quote": 0.0,
                "final_at": None,
                "terminal_reason": None,
                "exec_ids": [],
            },
        }

    def _place_order(self, pair: str, trade: dict, state: dict) -> bool:
        """Place an order via kraken-cli and write the initial journal entry.

        On success: returns True, writes a PLACED-state entry, and registers
        the order with self.execution_stream so subsequent WS events
        finalize its lifecycle asynchronously via _apply_execution_event.

        On any pre-exchange failure (ticker/validate/placement rejected):
        returns False, writes a terminal PLACEMENT_FAILED entry, and the
        caller rolls back the engine's pre-trade snapshot.

        Post-placement failures (post-only reject, DMS cancel, partial
        fills) are handled asynchronously by the execution stream — NOT
        here — on subsequent ticks.
        """
        if self.paper:
            return self._place_paper_order(pair, trade, state)

        amount = trade["amount"]
        action_upper = trade["action"].upper()
        action = action_upper.lower()
        entry = self._build_journal_entry(pair, trade, state)
        pre_trade_snap = state.get("_pre_trade_snapshot") if isinstance(state, dict) else None

        # ─── Real-balance preflight ─────────────────────────────────────
        # The engine sizes orders against its internal bookkeeping balance,
        # which may not reflect actual exchange holdings — especially for
        # non-USD-quoted pairs like SOL/BTC where the engine's BTC balance
        # is derived from a USD split, not real BTC on the account.
        # Check the actual currency balance before burning API calls.
        if action == "buy":
            quote = pair.split("/")[1]
            real_bal = self._get_real_quote_balance(quote)
            if real_bal is not None:
                cost_estimate = amount * (trade.get("price", 0) or 0)
                costmin = PositionSizer.MIN_COST.get(quote, 0.5)
                if real_bal < costmin or (cost_estimate > 0 and real_bal < cost_estimate):
                    is_usd = quote in ("USDC", "USD")
                    fmt = f"${real_bal:,.2f}" if is_usd else f"{real_bal:.8f}"
                    cost_fmt = f"${cost_estimate:,.2f}" if is_usd else f"{cost_estimate:.8f}"
                    engine = self.engines.get(pair)
                    # Post-v2.11.0 this path should be unreachable for non-
                    # USD-quoted pairs — the engine's `tradable` flag and
                    # _refresh_tradable_flags() combine to prevent sizing
                    # against a phantom balance. If we're here with
                    # tradable=True, it's a race with BalanceStream or a
                    # regression; surface sharply so it's easy to spot.
                    if engine is not None and getattr(engine, "tradable", True) and quote not in ("USDC", "USD"):
                        print(f"  [TRADE] Unexpected insufficient {quote} balance on "
                              f"tradable=True engine {pair} — likely BalanceStream race "
                              f"or regression. real={fmt} cost={cost_fmt}")
                    else:
                        print(f"  [TRADE] Insufficient {quote} balance ({fmt}) for {pair} "
                              f"BUY cost ~{cost_fmt} — skipping")
                    self._finalize_failed_entry(
                        entry, terminal_reason=f"insufficient_{quote}_balance",
                    )
                    return False
        elif action == "sell":
            base = pair.split("/")[0]
            real_base_bal = self._get_real_quote_balance(base)
            if real_base_bal is not None:
                min_size = PositionSizer.MIN_ORDER_SIZE.get(base, 0.02)
                if real_base_bal < min_size:
                    print(f"  [TRADE] Insufficient {base} balance "
                          f"({real_base_bal:.8f}) for {pair} SELL — "
                          f"below ordermin ({min_size}) — skipping")
                    self._finalize_failed_entry(
                        entry, terminal_reason=f"insufficient_{base}_balance",
                    )
                    return False
                if real_base_bal < amount:
                    print(f"  [TRADE] {pair} SELL: exchange {base} balance "
                          f"({real_base_bal:.8f}) < engine amount "
                          f"({amount:.8f}) — clamping to exchange balance")
                    amount = real_base_bal
                    trade["amount"] = amount
                    entry["intent"]["amount"] = amount

        # ─── Ticker fetch (WS stream only — refuse to trade without live price) ───
        ticker = self.ticker_stream.latest_ticker(pair) if self.ticker_stream.healthy else None
        if not ticker or "bid" not in ticker:
            print(f"  [TRADE] TickerStream has no bid/ask for {pair} — refusing to trade")
            self._finalize_failed_entry(
                entry, terminal_reason="ticker_stream_unavailable",
            )
            return False

        limit_price = ticker["bid"] if action == "buy" else ticker["ask"]
        entry["intent"]["limit_price"] = limit_price

        # ─── Validate ───
        time.sleep(2)
        print(f"  [TRADE] Validating {action_upper} {amount:.8f} {pair} @ {limit_price} (post-only limit)...")
        if action == "buy":
            val_result = KrakenCLI.order_buy(pair, amount, price=limit_price, validate=True)
        else:
            val_result = KrakenCLI.order_sell(pair, amount, price=limit_price, validate=True)
        if "error" in val_result:
            print(f"  [TRADE] Validation failed: {val_result['error']}")
            self._finalize_failed_entry(
                entry, terminal_reason=f"validation_failed:{val_result['error']}",
            )
            return False

        # ─── Re-fetch ticker (price may have drifted during validate) ───
        fresh_ticker = self.ticker_stream.latest_ticker(pair) or {}
        if "error" not in fresh_ticker and "bid" in fresh_ticker:
            limit_price = fresh_ticker["bid"] if action == "buy" else fresh_ticker["ask"]
            entry["intent"]["limit_price"] = limit_price

        # ─── Place for real ───
        userref = self._next_userref()
        print(f"  [TRADE] Placing {action_upper} {amount:.8f} {pair} @ {limit_price} "
              f"(limit post-only, userref={userref})...")
        if action == "buy":
            result = KrakenCLI.order_buy(pair, amount, price=limit_price, userref=userref)
        else:
            result = KrakenCLI.order_sell(pair, amount, price=limit_price, userref=userref)

        if "error" in result:
            print(f"  [TRADE] FAILED: {result['error']}")
            self._finalize_failed_entry(
                entry, terminal_reason=f"placement_error:{result['error']}",
            )
            return False

        # ─── Accepted: extract order_id, register with stream, append PLACED ───
        order_id = result.get("txid", result.get("result", {}).get("txid", "unknown"))
        if isinstance(order_id, list):
            order_id = order_id[0] if order_id else "unknown"
        print(f"  [TRADE] PLACED: {action_upper} {amount:.8f} {pair} | order_id: {order_id}")

        entry["order_ref"] = {"order_userref": userref, "order_id": order_id}
        self.order_journal.append(entry)
        journal_index = len(self.order_journal) - 1

        # Register with the execution stream so WS events can finalize this
        # order's lifecycle on subsequent ticks. Orders that come back as
        # order_id='unknown' cannot be correlated by id; register() is a
        # no-op in that case and the entry will stay at PLACED until manual
        # audit (rare — Kraken almost always returns a txid on success).
        self.execution_stream.register(
            order_id=order_id, userref=userref, journal_index=journal_index,
            pair=pair, side=action_upper, placed_amount=amount,
            engine_ref=self.engines[pair],
            pre_trade_snapshot=pre_trade_snap,
        )
        return True

    def _place_paper_order(self, pair: str, trade: dict, state: dict) -> bool:
        """Place a paper-mode order via `kraken paper`. Writes a journal
        entry that skips the WS-stream lifecycle entirely — paper trades
        synthesize their own terminal fill event which the next tick's
        drain_events() applies exactly like a real fill. This keeps the
        single code path between live and paper.
        """
        amount = trade["amount"]
        action_upper = trade["action"].upper()
        action = action_upper.lower()
        entry = self._build_journal_entry(pair, trade, state)

        time.sleep(2)
        print(f"  [PAPER] Placing {action_upper} {amount:.8f} {pair} (paper market)...")
        if action == "buy":
            result = KrakenCLI.paper_buy(pair, amount)
        else:
            result = KrakenCLI.paper_sell(pair, amount)
        if "error" in result:
            print(f"  [PAPER] FAILED: {result['error']}")
            self._finalize_failed_entry(
                entry, terminal_reason=f"paper_failed:{result['error']}",
            )
            return False

        # Success — paper fills at the requested limit_price. Append the
        # entry as PLACED first (so it has a journal index), then synthesize
        # a FILLED execution event for the stream to emit on drain.
        print(f"  [PAPER] PLACED: {action_upper} {amount:.8f} {pair}")
        # Build a deterministic pseudo order_id for paper correlation.
        paper_order_id = f"PAPER-{int(time.time() * 1e6)}"
        paper_userref = self._next_userref()
        entry["order_ref"] = {"order_userref": paper_userref, "order_id": paper_order_id}
        self.order_journal.append(entry)
        journal_index = len(self.order_journal) - 1
        self.execution_stream.register(
            order_id=paper_order_id, userref=paper_userref, journal_index=journal_index,
            pair=pair, side=action_upper, placed_amount=amount,
            engine_ref=self.engines[pair],
            pre_trade_snapshot=state.get("_pre_trade_snapshot") if isinstance(state, dict) else None,
        )
        limit_price = entry["intent"]["limit_price"] or float(trade.get("price") or 0)
        synthetic_fill = {
            "exec_type": "trade",
            "exec_id": f"{paper_order_id}-fill",
            "order_id": paper_order_id,
            "order_status": "filled",
            "last_qty": amount,
            "last_price": limit_price,
            "cost": amount * limit_price,
            "fees": [],
            "order_userref": paper_userref,
            "side": action,
            "symbol": pair,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
        self.execution_stream.inject_event(synthetic_fill)
        return True

    def _finalize_failed_entry(self, entry: Dict[str, Any], *, terminal_reason: str) -> None:
        """Patch a journal entry to PLACEMENT_FAILED and append it. Used
        for pre-exchange failures (ticker/validate/placement rejected)."""
        entry["lifecycle"] = {
            "state": "PLACEMENT_FAILED",
            "vol_exec": 0.0,
            "avg_fill_price": None,
            "fee_quote": 0.0,
            "final_at": datetime.now(timezone.utc).isoformat(),
            "terminal_reason": terminal_reason,
            "exec_ids": [],
        }
        self.order_journal.append(entry)

    def _reconcile_stale_placed(self):
        """Query exchange for PLACED journal entries that have no ExecutionStream
        registration — typically orders from a previous session that finalized
        while we were offline.

        For terminal orders (closed/canceled/expired): updates journal lifecycle
        directly. Engine rollback is NOT possible for entries from previous
        sessions (no pre_trade_snapshot persisted), so we log a warning.

        For still-open orders: registers them with the live ExecutionStream so
        WS events can finalize them normally.
        """
        # Collect PLACED entries with queryable order IDs
        stale = []
        for idx, entry in enumerate(self.order_journal):
            lifecycle = entry.get("lifecycle", {})
            if lifecycle.get("state") != "PLACED":
                continue
            order_id = entry.get("order_ref", {}).get("order_id")
            if not order_id or order_id == "unknown":
                continue
            stale.append((idx, entry, order_id))

        if not stale:
            return

        print(f"  [HYDRA] Reconciling {len(stale)} stale PLACED journal entries...")

        # Dedup order_ids (shouldn't have duplicates, but be safe)
        seen_ids = set()
        unique_stale = []
        for idx, entry, oid in stale:
            if oid not in seen_ids:
                seen_ids.add(oid)
                unique_stale.append((idx, entry, oid))

        # Batch query exchange
        BATCH = 20
        order_ids = [oid for _, _, oid in unique_stale]
        # Build lookup: order_id → (journal_index, entry)
        oid_to_entry = {oid: (idx, entry) for idx, entry, oid in unique_stale}

        reconciled = 0
        registered = 0
        for i in range(0, len(order_ids), BATCH):
            batch = order_ids[i:i + BATCH]
            time.sleep(2)  # Rate limit
            resp = KrakenCLI.query_orders(*batch, trades=True)
            if not isinstance(resp, dict) or "error" in resp:
                print(f"  [WARN] stale-placed query failed: {resp}")
                continue

            for txid, order_info in resp.items():
                if not isinstance(order_info, dict):
                    continue
                if txid not in oid_to_entry:
                    continue
                idx, entry = oid_to_entry[txid]
                status = order_info.get("status", "")

                if status in ("closed", "canceled", "expired"):
                    # Terminal — finalize journal entry
                    vol_exec = float(order_info.get("vol_exec", 0))
                    placed = entry.get("intent", {}).get("amount", 0)
                    raw_price = float(order_info.get("price", 0))
                    avg_price = raw_price if raw_price > 0 else None
                    fee = float(order_info.get("fee", 0))

                    if status == "closed":
                        state = (
                            "FILLED"
                            if _is_fully_filled(vol_exec, placed)
                            else "PARTIALLY_FILLED"
                        )
                    elif vol_exec > 0:
                        state = "PARTIALLY_FILLED"
                    else:
                        state = "CANCELLED_UNFILLED"

                    entry["lifecycle"] = {
                        "state": state,
                        "vol_exec": vol_exec,
                        "avg_fill_price": avg_price,
                        "fee_quote": fee,
                        "final_at": order_info.get("closetm") or datetime.now(timezone.utc).isoformat(),
                        "terminal_reason": f"reconciled on resume ({status})",
                        "exec_ids": [],
                    }
                    pair = entry.get("pair", "?")
                    side = entry.get("side", "?")
                    print(f"  [HYDRA] {pair} {side} {txid}: {state} "
                          f"(vol={vol_exec:.8f}, reconciled on resume)")

                    # Previous-session fills have no pre_trade_snapshot (not
                    # persisted). We use the arithmetic fallback in
                    # reconcile_partial_fill for PARTIALLY_FILLED, accepting
                    # minor avg_entry drift if the original trade was an
                    # average-in. For fully unfilled, log — operator verifies.
                    if state == "PARTIALLY_FILLED":
                        engine = self.engines.get(pair)
                        placed = float(entry.get("intent", {}).get("amount", 0) or 0)
                        limit_px = avg_price if avg_price else float(
                            entry.get("intent", {}).get("limit_price", 0) or 0
                        )
                        if engine and placed > 0 and limit_px > 0:
                            try:
                                engine.reconcile_partial_fill(
                                    side=side,
                                    placed_amount=placed,
                                    vol_exec=vol_exec,
                                    limit_price=limit_px,
                                    pre_trade_snapshot=None,
                                    reason=f"PARTIALLY_FILLED reconciled on resume ({txid})",
                                )
                                print(f"  [HYDRA] {pair} {side} engine adjusted "
                                      f"(arithmetic fallback; avg_entry may drift "
                                      f"slightly if original was an average-in)")
                            except Exception as e:
                                print(f"  [WARN] {pair} {side} partial-fill reconcile "
                                      f"failed ({e}); engine over-committed")
                    elif state in ("CANCELLED_UNFILLED", "REJECTED"):
                        print(f"  [WARN] {pair} {side} was never filled — engine position may be "
                              f"stale from snapshot. Operator should verify.")
                    reconciled += 1

                elif status in ("open", "pending", "pending_new", "new"):
                    # Still live on the exchange — register with ExecutionStream
                    # so the WS stream can finalize it normally.
                    pair = entry.get("pair", "")
                    side = entry.get("side", "")
                    userref = entry.get("order_ref", {}).get("order_userref")
                    placed_amount = entry.get("intent", {}).get("amount", 0)
                    engine = self.engines.get(pair)
                    if engine and pair:
                        self.execution_stream.register(
                            order_id=txid,
                            userref=userref,
                            journal_index=idx,
                            pair=pair,
                            side=side,
                            placed_amount=float(placed_amount),
                            engine_ref=engine,
                            pre_trade_snapshot=None,  # unavailable after restart
                        )
                        registered += 1
                        print(f"  [HYDRA] {pair} {side} {txid}: still open — "
                              f"registered with execution stream")

        parts = []
        if reconciled:
            parts.append(f"{reconciled} finalized")
        if registered:
            parts.append(f"{registered} re-registered")
        if parts:
            print(f"  [HYDRA] Stale PLACED reconciliation: {', '.join(parts)}")
        else:
            print(f"  [HYDRA] Stale PLACED reconciliation: all {len(stale)} entries still pending on exchange or query failed")

    def _apply_execution_event(self, event: Dict[str, Any]) -> None:
        """Apply one terminal event from the execution stream to the
        journal entry it came from AND the engine state. Called in the
        tick loop after drain_events()."""
        idx = event.get("journal_index")
        order_id = event.get("order_id")
        entry = None

        # Primary: try index if it's still valid and matches the order_id
        if isinstance(idx, int) and 0 <= idx < len(self.order_journal):
            candidate = self.order_journal[idx]
            cand_oid = candidate.get("order_ref", {}).get("order_id")
            if cand_oid == order_id:
                entry = candidate

        # Fallback: reverse-scan by order_id (handles journal trimming)
        if entry is None and order_id:
            for e in reversed(self.order_journal):
                if e.get("order_ref", {}).get("order_id") == order_id:
                    entry = e
                    break

        if entry is None:
            print(f"  [EXEC] journal entry not found for order_id={order_id} "
                  f"idx={idx} — event dropped")
            return
        state_name = event["state"]
        entry["lifecycle"] = {
            "state": state_name,
            "vol_exec": event["vol_exec"],
            "avg_fill_price": event.get("avg_fill_price"),
            "fee_quote": event.get("fee_quote") or 0.0,
            "final_at": event.get("timestamp") or datetime.now(timezone.utc).isoformat(),
            "terminal_reason": event.get("terminal_reason"),
            "exec_ids": event.get("exec_ids") or [],
        }

        engine = event.get("engine_ref")
        pre_snap = event.get("pre_trade_snapshot")
        pair = event.get("pair")
        side = event.get("side")
        placed_amount = event.get("placed_amount") or 0.0
        vol_exec = event.get("vol_exec") or 0.0

        if state_name == "FILLED":
            # Engine was optimistically committed at placement time — no
            # correction needed.
            return
        if state_name in ("CANCELLED_UNFILLED", "REJECTED"):
            if engine is not None and pre_snap is not None:
                engine.restore_position(pre_snap)
                print(f"  [EXEC] {pair} {side} {state_name}: engine rolled back "
                      f"(reason: {event.get('terminal_reason') or 'n/a'})")
            return
        if state_name == "PARTIALLY_FILLED":
            # Engine was optimistically committed to the full placed_amount;
            # actual fill was only vol_exec. reconcile_partial_fill restores
            # to the pre-trade snapshot and replays only the vol_exec portion,
            # leaving engine state indistinguishable from a world in which
            # execute_signal had been called with the real fill amount.
            ratio = (vol_exec / placed_amount) if placed_amount > 0 else 0.0
            limit_price = float(event.get("avg_fill_price") or 0.0)
            if limit_price <= 0 and engine is not None and engine.prices:
                # Fallback when Kraken didn't report an avg_fill_price
                limit_price = engine.prices[-1]
            if engine is not None:
                try:
                    engine.reconcile_partial_fill(
                        side=side or "",
                        placed_amount=float(placed_amount),
                        vol_exec=float(vol_exec),
                        limit_price=limit_price,
                        pre_trade_snapshot=pre_snap,
                        reason=f"PARTIALLY_FILLED: {event.get('terminal_reason') or ''}",
                    )
                    print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                          f"filled {vol_exec:.8f}/{placed_amount:.8f} ({ratio:.1%}) — "
                          f"engine reconciled to actual fill")
                except Exception as e:
                    print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                          f"reconcile failed ({e}); engine may be over-committed")
            else:
                print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                      f"filled {vol_exec:.8f}/{placed_amount:.8f} ({ratio:.1%}) — "
                      f"no engine_ref; journal carries truth but engine is stale")
            return

    def _run_tuner_update(self):
        """Run Bayesian parameter update across all pair trackers."""
        for pair in self.pairs:
            tracker = self.trackers[pair]
            if len(tracker.observations) < 20:
                continue
            old_params = tracker.get_tunable_params()
            new_params = tracker.update()
            changes = tracker.get_changes_log(old_params)
            if changes:
                print(f"  [TUNER] {pair}: parameter update #{tracker.update_count}")
                for line in changes:
                    print(f"  [TUNER] {line}")
                # Apply to engine
                self.engines[pair].apply_tuned_params(new_params)
        self._completed_trades_since_update = 0

    def _execute_coordinated_swap(self, swap: dict, all_states: dict):
        """Execute a coordinated cross-pair swap (sell one pair, buy another).

        Generates two trades as an atomic unit with a shared swap_id.
        Executes the sell leg first, then the buy leg. If the buy leg cannot
        proceed after the sell has been placed on the exchange, the resting
        sell is cancelled so the swap is not left half-executed — the
        resulting CANCELLED_UNFILLED event rolls back the engine via
        _apply_execution_event. Pre-flight checks (buy_engine exists,
        buy_price > 0) run before the sell placement so common failures
        don't reach the exchange at all.
        """
        sell_pair = swap["sell_pair"]
        buy_pair = swap["buy_pair"]
        reason = swap["reason"]

        sell_state = all_states.get(sell_pair)
        buy_state = all_states.get(buy_pair)
        if not sell_state or not buy_state:
            print(f"  [SWAP] Cannot execute swap: missing state for {sell_pair} or {buy_pair}")
            return

        sell_engine = self.engines.get(sell_pair)
        if not sell_engine or sell_engine.position.size <= 0:
            print(f"  [SWAP] No position to sell on {sell_pair}, skipping swap")
            return

        # Pre-flight buy-leg checks — catch deterministic failures BEFORE
        # placing the sell so we don't leave an orphan sell on the exchange.
        buy_engine = self.engines.get(buy_pair)
        if not buy_engine:
            print(f"  [SWAP] No engine for {buy_pair}, skipping swap (pre-flight)")
            return
        if buy_state.get("price", 0) <= 0:
            print(f"  [SWAP] No price for {buy_pair}, skipping swap (pre-flight)")
            return

        self._swap_counter += 1
        swap_id = f"swap_{self._swap_counter}_{int(time.time())}"
        sell_amount = sell_engine.position.size
        sell_price = sell_state.get("price", 0)

        print(f"  [SWAP] Coordinated swap {swap_id}: SELL {sell_amount:.8f} {sell_pair} → BUY {buy_pair}")
        print(f"  [SWAP] Reason: {reason}")

        # Leg 1: Sell — update engine state first, then execute on exchange
        sell_snap = sell_engine.snapshot_position()
        sell_trade_obj = sell_engine.execute_signal(
            action="SELL", confidence=0.85,
            reason=f"[SWAP {swap_id}] Sell leg: {reason}",
            strategy=sell_state.get("strategy", "MOMENTUM"),
        )
        if not sell_trade_obj:
            print(f"  [SWAP] Engine rejected sell on {sell_pair}, skipping swap")
            return

        sell_trade = {
            "action": "SELL",
            "amount": sell_trade_obj.amount,
            "price": sell_trade_obj.price,
            "reason": sell_trade_obj.reason,
            "confidence": 0.85,
            "swap_id": swap_id,
        }
        sell_state["_pre_trade_snapshot"] = sell_snap
        if not self._place_order(sell_pair, sell_trade, sell_state):
            sell_engine.restore_position(sell_snap)
            print(f"  [ROLLBACK] {sell_pair}: engine state rolled back after failed swap sell")
            return

        # Capture the sell order_id so we can cancel it if the buy leg fails.
        # After a successful _place_order, the most recent journal entry is
        # ours (single-threaded tick loop). Matched by pair+side+swap_id to
        # defend against any unexpected ordering.
        sell_order_id: Optional[str] = None
        for entry in reversed(self.order_journal):
            if (entry.get("pair") == sell_pair
                    and entry.get("side") == "SELL"
                    and (entry.get("decision") or {}).get("swap_id") == swap_id):
                sell_order_id = (entry.get("order_ref") or {}).get("order_id")
                break

        def _cancel_orphan_sell(why: str) -> None:
            """Cancel the in-flight sell on exchange if the buy leg can't proceed.

            Engine rollback happens automatically when cancellation propagates
            through the execution stream as CANCELLED_UNFILLED (see
            _apply_execution_event). We don't restore the engine manually
            because the sell could have partially filled between placement
            and cancellation — the stream's terminal event carries the
            authoritative vol_exec.

            In paper mode the sell was filled synthetically at placement
            time, so there is nothing to cancel on the exchange — we just
            log the unbalanced swap so the operator can see it.
            """
            if self.paper:
                print(f"  [SWAP] WARNING: paper sell already synthesized as filled; "
                      f"swap {swap_id} half-executed ({why})")
                return
            if not sell_order_id or sell_order_id == "unknown":
                print(f"  [SWAP] WARNING: no order_id captured for sell leg; "
                      f"cannot cancel orphan ({why})")
                return
            try:
                time.sleep(2)  # rate limit
                cancel_result = KrakenCLI.cancel_order(sell_order_id)
                if isinstance(cancel_result, dict) and "error" in cancel_result:
                    print(f"  [SWAP] WARNING: cancel orphan sell {sell_order_id} "
                          f"failed: {cancel_result['error']} ({why}). "
                          f"Sell may have filled before cancel; check journal.")
                else:
                    print(f"  [SWAP] Cancelled orphan sell {sell_order_id} ({why}). "
                          f"Engine rollback will complete when CANCELLED_UNFILLED "
                          f"event drains.")
            except Exception as e:
                print(f"  [SWAP] WARNING: cancel orphan sell {sell_order_id} "
                      f"raised {type(e).__name__}: {e} ({why})")

        # Leg 2: Buy on the target pair. Re-read price in case it drifted
        # during the sell placement's rate-limit sleeps.
        buy_price = buy_state.get("price", 0)
        if buy_price <= 0:
            _cancel_orphan_sell("buy price disappeared after sell placement")
            return

        # Engine sizes the buy via Kelly criterion — execute_signal handles
        # position sizing, balance check, and minimum order enforcement internally.
        buy_snap = buy_engine.snapshot_position()
        buy_trade_obj = buy_engine.execute_signal(
            action="BUY", confidence=0.85,
            reason=f"[SWAP {swap_id}] Buy leg: {reason}",
            strategy=buy_state.get("strategy", "MOMENTUM"),
        )
        if not buy_trade_obj:
            _cancel_orphan_sell("engine rejected buy (halted or insufficient balance)")
            return

        # Use the engine's actual executed amount for the exchange order
        buy_trade = {
            "action": "BUY",
            "amount": buy_trade_obj.amount,
            "price": buy_trade_obj.price,
            "reason": buy_trade_obj.reason,
            "confidence": 0.85,
            "swap_id": swap_id,
        }
        buy_state["_pre_trade_snapshot"] = buy_snap
        if not self._place_order(buy_pair, buy_trade, buy_state):
            buy_engine.restore_position(buy_snap)
            print(f"  [ROLLBACK] {buy_pair}: engine state rolled back after failed swap buy")
            _cancel_orphan_sell("_place_order failed for buy leg")
            return

        # Both legs placed — the swap_id tag on each leg's journal entry
        # is how callers link them back together. No separate marker row.
        print(f"  [SWAP] Swap {swap_id} placed (both legs; lifecycle via execution stream)")

    def _log_regime_transitions(self, all_states: Dict[str, dict]):
        """Log regime transitions across pairs for observability.
        Actionable cross-pair overrides are handled by CrossPairCoordinator in Phase 1.5.
        """
        for pair, state in all_states.items():
            current_regime = state.get("regime", "RANGING")
            prev_regime = self.prev_regimes.get(pair)

            if prev_regime and prev_regime != current_regime:
                print(f"  [REGIME] {pair}: {prev_regime} -> {current_regime}")

                # Opportunistic cross-pair logic:
                # If SOL/USDC shifts to TREND_DOWN and we hold SOL, consider selling SOL for BTC
                if pair == "SOL/USDC" and current_regime == "TREND_DOWN":
                    if "SOL/BTC" in all_states:
                        btc_regime = all_states["SOL/BTC"].get("regime")
                        if btc_regime in ("TREND_UP", "RANGING"):
                            print(f"  [REGIME] Cross-pair opportunity: SOL weakening vs USDC but "
                                  f"SOL/BTC is {btc_regime} — consider selling SOL for BTC")

                # If SOL/USDC shifts to TREND_UP, consider buying SOL with USDC
                if pair == "SOL/USDC" and current_regime == "TREND_UP":
                    print(f"  [REGIME] SOL trending up — MOMENTUM strategy active")

            self.prev_regimes[pair] = current_regime

    def _set_engine_balances(self, per_pair_usd: float):
        """Set engine balances and the per-engine `tradable` flag.

        USDC/USD-quoted pairs get a 1/N slice of the tradable USD balance.

        Non-USD-quoted pairs (e.g. SOL/BTC) previously received a USD→quote
        converted slice, which produced a "phantom" balance when the account
        held none of the quote currency. That phantom balance caused the
        engine to size and attempt orders it could never actually place,
        triggering a loop of `PLACEMENT_FAILED: insufficient_{quote}_balance`
        entries (see v2.11.0 CHANGELOG).

        Fixed policy:
          • Balance = real exchange holding of the quote currency (not a
            USD-derived estimate).
          • `tradable = True` iff the real holding exceeds costmin for that
            quote — otherwise the engine is `tradable=False` (signal still
            generated for Rule 4 confluence, but no Trade is ever produced).

        When an engine already holds a position (e.g. from --resume), we set
        initial_balance = cash + position_value so that P&L starts at 0% from
        the point of the balance reset, rather than showing a bogus gain from
        the position being valued against a tiny converted initial balance.
        """
        prices = self._get_asset_prices()
        for pair in self.pairs:
            engine = self.engines[pair]
            quote = pair.split("/")[1]
            current_price = engine.prices[-1] if engine.prices else 0
            if quote in ("USDC", "USD"):
                equity = per_pair_usd + engine.position.size * current_price
                engine.balance = per_pair_usd
                engine.initial_balance = equity
                engine.peak_equity = equity
                engine.tradable = True
                continue

            # Paper mode: keep the legacy USD→quote conversion so strategy
            # simulations are not artificially gated by on-account holdings.
            # Paper users are testing the thesis, not funding constraints.
            if self.paper:
                if quote in prices and prices[quote] > 0:
                    balance_quote = per_pair_usd / prices[quote]
                    equity = balance_quote + engine.position.size * current_price
                    engine.balance = balance_quote
                    engine.initial_balance = equity
                    engine.peak_equity = equity
                else:
                    equity = per_pair_usd + engine.position.size * current_price
                    engine.balance = per_pair_usd
                    engine.initial_balance = equity
                    engine.peak_equity = equity
                engine.tradable = True
                continue

            # Live mode, non-USD quote: use the real exchange balance.
            real_quote = self._get_real_quote_balance(quote) or 0.0
            costmin = PositionSizer.MIN_COST.get(quote, 0.0)
            if real_quote > costmin:
                equity = real_quote + engine.position.size * current_price
                engine.balance = real_quote
                engine.initial_balance = equity
                engine.peak_equity = equity
                engine.tradable = True
                print(f"  [HYDRA] {pair}: tradable — real balance {real_quote:.8f} {quote} "
                      f"(equity {equity:.8f})")
            else:
                # Informational-only: engine ticks normally, surfaces
                # regime + signal for confluence, but _maybe_execute
                # short-circuits so no placement is attempted.
                equity = engine.position.size * current_price
                engine.balance = 0.0
                engine.initial_balance = equity if equity > 0 else 0.0
                engine.peak_equity = engine.initial_balance
                engine.tradable = False
                print(f"  [HYDRA] {pair}: informational-only — no {quote} held "
                      f"(balance {real_quote:.8f}, costmin {costmin})")

    def _refresh_tradable_flags(self) -> None:
        """Re-evaluate the `tradable` flag for every engine once per tick.

        Cheap: reads the latest BalanceStream snapshot (push-based, no
        REST call). Transitions are logged exactly once (False→True and
        True→False). When a pair flips False→True — e.g. a BTC/USDC BUY
        just filled, so we now hold BTC — the engine's balance and equity
        baseline are re-seeded from the real holding so its circuit
        breaker and P&L calculations start clean from that point.

        USDC/USD-quoted pairs are skipped because their tradability
        depends on the shared tradable USD pool, not on holding a
        specific currency.
        """
        for pair in self.pairs:
            engine = self.engines[pair]
            quote = pair.split("/")[1]
            if quote in ("USDC", "USD"):
                if not engine.tradable:
                    # USD pairs should never be informational-only; if they
                    # somehow are, re-enable them. Balance unchanged.
                    engine.tradable = True
                continue
            real_quote = self._get_real_quote_balance(quote) or 0.0
            costmin = PositionSizer.MIN_COST.get(quote, 0.0)
            should_be_tradable = real_quote > costmin
            if should_be_tradable and not engine.tradable:
                current_price = engine.prices[-1] if engine.prices else 0
                equity = real_quote + engine.position.size * current_price
                engine.balance = real_quote
                engine.initial_balance = equity
                engine.peak_equity = equity
                engine.max_drawdown = 0.0
                engine.equity_history = []
                engine.tradable = True
                print(f"  [HYDRA] {pair}: ACTIVATED — real {quote} balance "
                      f"{real_quote:.8f} available (equity {equity:.8f})")
            elif not should_be_tradable and engine.tradable:
                engine.balance = 0.0
                engine.tradable = False
                print(f"  [HYDRA] {pair}: DEACTIVATED — {quote} balance depleted "
                      f"({real_quote:.8f} < costmin {costmin})")

    def _get_asset_prices(self) -> dict:
        """Get current USD prices for known assets from engine state.
        Returns {canonical_asset: usd_price}."""
        prices = {"USDC": 1.0, "USD": 1.0}
        for pair, engine in self.engines.items():
            if engine.prices:
                base, quote = pair.split("/")
                if quote in ("USDC", "USD"):
                    prices[base] = engine.prices[-1]
        # Derive BTC price from SOL/BTC if BTC/USDC not available
        if "BTC" not in prices and "SOL" in prices:
            sol_btc_engine = self.engines.get("SOL/BTC")
            if sol_btc_engine and sol_btc_engine.prices:
                sol_per_btc = sol_btc_engine.prices[-1]
                if sol_per_btc > 0:
                    prices["BTC"] = prices["SOL"] / sol_per_btc
        return prices

    def _get_real_quote_balance(self, quote: str) -> Optional[float]:
        """Return the actual exchange balance for a quote currency.

        Prefers the real-time BalanceStream; falls back to the cached REST
        balance from startup.  Returns None only if no balance data is
        available at all (should not happen after warmup).
        """
        bal = None
        if not self.paper and self.balance_stream.healthy:
            bal = self.balance_stream.latest_balances()
        if not bal:
            bal = getattr(self, "_cached_balance", None)
        if not bal:
            return None
        # Sum all non-staked holdings that normalize to the quote currency.
        total = 0.0
        for asset, amount in bal.items():
            if KrakenCLI._is_staked(asset):
                continue
            if KrakenCLI._normalize_asset(asset) == quote:
                total += amount
        return total

    def _extract_fee_tier(self, vol_response: dict) -> dict:
        """Normalize a `kraken volume` response into a compact fee-tier dict.

        Returns {"volume_30d_usd": float|None, "pair_fees": {friendly_pair: {"maker_pct","taker_pct"}}}.
        Defensive: any missing / malformed sub-field is silently coerced to None
        instead of raising, so a transient Kraken shape change cannot crash the tick.
        """
        result = {"volume_30d_usd": None, "pair_fees": {}}
        if not isinstance(vol_response, dict):
            return result
        try:
            v = vol_response.get("volume")
            if v is not None:
                result["volume_30d_usd"] = float(v)
        except (TypeError, ValueError):
            pass
        fees_taker = vol_response.get("fees") or {}
        fees_maker = vol_response.get("fees_maker") or {}
        if not isinstance(fees_taker, dict):
            fees_taker = {}
        if not isinstance(fees_maker, dict):
            fees_maker = {}
        # Kraken may return fee keys in several forms ("SOLUSDC", "SOL/USDC", "BTCUSDC",
        # "XXBTZUSD" historically). Build a forgiving reverse map that accepts both the
        # PAIR_MAP-resolved form and the slashless form of the original friendly pair.
        pair_reverse = {}
        for p in getattr(self, "pairs", []):
            resolved = KrakenCLI._resolve_pair(p)
            pair_reverse[resolved] = p
            pair_reverse[p.replace("/", "")] = p  # slashless fallback ("SOLUSDC" → "SOL/USDC")
            pair_reverse[p] = p                   # passthrough
        # Add legacy XBT alias forms so Kraken fee keys like "XBTUSDC", "SOLXBT" resolve
        for alias, target in KrakenCLI.PAIR_MAP.items():
            if alias != target:
                for p in getattr(self, "pairs", []):
                    if KrakenCLI._resolve_pair(p) == target:
                        pair_reverse[alias] = p
                        pair_reverse[alias.replace("/", "")] = p
                        break
        seen_keys = set(fees_taker.keys()) | set(fees_maker.keys())
        for raw_key in seen_keys:
            friendly = pair_reverse.get(raw_key, raw_key)
            taker_entry = fees_taker.get(raw_key) or {}
            maker_entry = fees_maker.get(raw_key) or {}
            taker_pct = None
            maker_pct = None
            if isinstance(taker_entry, dict):
                try:
                    val = taker_entry.get("fee")
                    if val is not None:
                        taker_pct = float(val)
                except (TypeError, ValueError):
                    taker_pct = None
            if isinstance(maker_entry, dict):
                try:
                    val = maker_entry.get("fee")
                    if val is not None:
                        maker_pct = float(val)
                except (TypeError, ValueError):
                    maker_pct = None
            result["pair_fees"][friendly] = {"maker_pct": maker_pct, "taker_pct": taker_pct}
        return result

    def _compute_balance_usd(self, balance: dict) -> dict:
        """Convert raw Kraken balance to USD breakdown with staked asset handling.

        Returns {
            "total_usd": float,      # All assets in USD
            "tradable_usd": float,   # Only tradable (non-staked) assets
            "staked_usd": float,     # Staked/bonded/locked assets
            "assets": [{"asset": str, "amount": float, "usd_value": float, "staked": bool}, ...]
        }
        """
        prices = self._get_asset_prices()
        assets = []
        total_usd = 0.0
        tradable_usd = 0.0
        staked_usd = 0.0

        for asset, amount in balance.items():
            staked = KrakenCLI._is_staked(asset)
            canonical = KrakenCLI._normalize_asset(asset)
            usd_price = prices.get(canonical, 0.0)
            usd_value = amount * usd_price

            assets.append({
                "asset": asset,
                "canonical": canonical,
                "amount": round(amount, 8),
                "usd_value": round(usd_value, 2),
                "staked": staked,
            })
            total_usd += usd_value
            if staked:
                staked_usd += usd_value
            else:
                tradable_usd += usd_value

        # Sort: tradable first, then staked; within each group alphabetical
        assets.sort(key=lambda a: (a["staked"], a["asset"]))

        return {
            "total_usd": round(total_usd, 2),
            "tradable_usd": round(tradable_usd, 2),
            "staked_usd": round(staked_usd, 2),
            "assets": assets,
        }

    def _build_dashboard_state(self, tick: int, all_states: dict,
                                elapsed: float) -> dict:
        """Build the full state dict for the dashboard WebSocket."""
        # Balance: prefer WS stream when healthy (real-time, no API call).
        # Fall back to REST polling every 5th tick.
        ws_bal = (
            self.balance_stream.latest_balances()
            if not self.paper and self.balance_stream.healthy
            else None
        )
        if ws_bal:
            self._cached_balance = ws_bal
        else:
            pass  # Use startup-cached balance until WS reconnects
        bal = getattr(self, '_cached_balance', {})

        # Fee tier refresh — at most once per hour, live mode only (paper has no fee data).
        # On failure we leave the cache stale and do NOT advance _fee_tier_fetched_at,
        # so the next tick will retry. Diagnostic-only: has no effect on trading.
        if not self.paper:
            now = time.time()
            if now - self._fee_tier_fetched_at > 3600:
                time.sleep(2)  # Rate limit
                vol = KrakenCLI.volume(self.pairs)
                if isinstance(vol, dict) and "error" not in vol:
                    self._fee_tier_cache = self._extract_fee_tier(vol)
                    self._fee_tier_fetched_at = now
                else:
                    err = vol.get("error") if isinstance(vol, dict) else str(vol)
                    print(f"  [FEES] volume fetch failed: {err}")

        # Compute USD-equivalent balance breakdown
        balance_usd = self._compute_balance_usd(bal) if bal else {
            "total_usd": 0, "tradable_usd": 0, "staked_usd": 0, "assets": []
        }

        pairs_data = {}
        for pair, state in all_states.items():
            pairs_data[pair] = state
            # Per-pair tradable flag — dashboard renders an INFO-ONLY
            # badge when False. Defaults to True if the engine is
            # missing (defensive: should not happen).
            engine = self.engines.get(pair)
            if state is not None:
                state["tradable"] = bool(getattr(engine, "tradable", True)) if engine else True

        # Journal-derived stats — wrapped in try/except so a malformed journal
        # entry can never crash the broadcast and blank the dashboard.
        journal_stats: Dict[str, Any] = {
            "total_fills": 0, "fills_by_pair": {}, "fill_win_rate": 0,
            "pnl_by_pair": {}, "total_realized_pnl_usd": 0,
            "total_unrealized_pnl_usd": 0, "total_pnl_usd": 0,
        }
        try:
            _FILL_STATES = ("FILLED", "PARTIALLY_FILLED")
            total_fills = 0
            fills_by_pair: Dict[str, Dict[str, Any]] = {}
            _buy_cost: Dict[str, float] = {}
            _buy_qty: Dict[str, float] = {}
            for entry in self.order_journal:
                lc = entry.get("lifecycle") or {}
                if lc.get("state") not in _FILL_STATES:
                    continue
                total_fills += 1
                p = entry.get("pair", "")
                if p not in fills_by_pair:
                    fills_by_pair[p] = {"buys": 0, "sells": 0, "sell_wins": 0, "sell_losses": 0}
                side = entry.get("side")
                vol = float(lc.get("vol_exec") or 0)
                price = float(lc.get("avg_fill_price") or (entry.get("intent") or {}).get("limit_price") or 0)
                if side == "BUY":
                    fills_by_pair[p]["buys"] += 1
                    _buy_cost[p] = _buy_cost.get(p, 0) + vol * price
                    _buy_qty[p] = _buy_qty.get(p, 0) + vol
                elif side == "SELL":
                    fills_by_pair[p]["sells"] += 1
                    avg_buy = (_buy_cost.get(p, 0) / _buy_qty[p]) if _buy_qty.get(p, 0) > 0 else 0
                    if avg_buy > 0 and price > 0:
                        if price >= avg_buy:
                            fills_by_pair[p]["sell_wins"] += 1
                        else:
                            fills_by_pair[p]["sell_losses"] += 1
                    sold_cost = vol * avg_buy if avg_buy > 0 else 0
                    _buy_cost[p] = max(0.0, _buy_cost.get(p, 0) - sold_cost)
                    _buy_qty[p] = max(0.0, _buy_qty.get(p, 0) - vol)
            total_sell_wins = sum(v.get("sell_wins", 0) for v in fills_by_pair.values())
            total_sell_losses = sum(v.get("sell_losses", 0) for v in fills_by_pair.values())
            total_sells = total_sell_wins + total_sell_losses
            fill_win_rate = round(total_sell_wins / total_sells * 100, 2) if total_sells > 0 else 0

            asset_prices = self._get_asset_prices()
            total_realized_pnl_usd = 0.0
            total_unrealized_pnl_usd = 0.0
            pnl_by_pair: Dict[str, Dict[str, float]] = {}
            for pair in self.pairs:
                realized = self._compute_pair_realized_pnl(pair)
                engine = self.engines.get(pair)
                ep = engine.prices[-1] if engine and engine.prices else 0
                unrealized = (engine.position.size * (ep - engine.position.avg_entry)
                              if engine and engine.position.size > 0 else 0)
                quote = pair.split("/")[1] if "/" in pair else "USD"
                quote_usd = asset_prices.get(quote, 1.0)
                pnl_by_pair[pair] = {
                    "realized": round(realized, 8),
                    "unrealized": round(unrealized, 8),
                    "net": round(realized + unrealized, 8),
                    "net_usd": round((realized + unrealized) * quote_usd, 2),
                }
                total_realized_pnl_usd += realized * quote_usd
                total_unrealized_pnl_usd += unrealized * quote_usd
            total_pnl_usd = total_realized_pnl_usd + total_unrealized_pnl_usd

            journal_stats = {
                "total_fills": total_fills,
                "fills_by_pair": fills_by_pair,
                "fill_win_rate": fill_win_rate,
                "pnl_by_pair": pnl_by_pair,
                "total_realized_pnl_usd": round(total_realized_pnl_usd, 2),
                "total_unrealized_pnl_usd": round(total_unrealized_pnl_usd, 2),
                "total_pnl_usd": round(total_pnl_usd, 2),
            }
        except Exception as e:
            print(f"  [WARN] journal_stats computation failed: {type(e).__name__}: {e}")

        return {
            "type": "state_update",
            "tick": tick,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "elapsed": round(elapsed, 1),
            "remaining": 0 if self.duration == 0 else round(self.duration - elapsed, 1),
            "balance": bal if "error" not in bal else {},
            "balance_usd": balance_usd,
            "fee_tier": self._fee_tier_cache,
            "pairs": pairs_data,
            "order_journal": self.order_journal[-20:],
            "journal_stats": journal_stats,
            "running": self.running,
            "interval": self.interval,
            "mode": self.mode,
            "ai_brain": self.brain.get_stats() if self.brain else None,
        }

    def _print_tick_status(self, pair: str, state: dict):
        """Print concise tick status."""
        s = state["signal"]
        p = state["portfolio"]
        pos = state["position"]
        is_usd = pair.endswith("USDC") or pair.endswith("USD")
        cur = "$" if is_usd else ""
        # BTC-quoted pairs trade at ~0.001–0.01; 4 decimals loses precision
        # (SOL/BTC ~0.00148 would render as "0.0015"). Use 8 decimals for
        # crypto-quoted pairs, 4 for USD/USDC pairs.
        pd = 4 if is_usd else 8

        signal_icon = {"BUY": "^", "SELL": "v", "HOLD": "-"}.get(s["action"], "?")

        print(f"  | {pair:<10} | {cur}{state['price']:>12,.{pd}f} | "
              f"{state['regime']:<10} -> {state['strategy']:<15} | "
              f"{signal_icon} {s['action']:<4} ({s['confidence']:.2f}) | "
              f"Eq: {cur}{p['equity']:>10,.{2 if is_usd else 8}f} | "
              f"P&L: {p['pnl_pct']:>+.2f}% | DD: {p['max_drawdown_pct']:.1f}%")

        if pos["size"] > 0:
            print(f"  |            | Pos: {pos['size']:.8f} @ {cur}{pos['avg_entry']:,.{pd}f} | "
                  f"Unrealized: {cur}{pos['unrealized_pnl']:>+,.{2 if is_usd else 8}f}")

        if state.get("ai_decision") and not state["ai_decision"].get("fallback"):
            ai = state["ai_decision"]
            print(f"  |  [AI] {ai['action']} → {ai['final_signal']} | {ai.get('summary', '')[:70]}")

        if state.get("last_trade"):
            t = state["last_trade"]
            _cur = "$" if is_usd else ""
            profit_str = f" | Profit: {_cur}{t['profit']:+,.{2 if is_usd else 8}f}" if t.get("profit") is not None else ""
            print(f"  |  >>> SIGNAL: {t['action']} {t['amount']:.8f} @ {_cur}{t['price']:,.{pd}f}{profit_str}")
            print(f"  |      Reason: {t['reason'][:75]}")

    def _print_banner(self):
        trade_mode = "PAPER" if self.paper else "LIVE"
        sizing_mode = self.mode.upper()
        brain_status = f"AI Brain: {self.brain.provider}/{self.brain.model}" if self.brain else "AI Brain: DISABLED (no API key)"
        print("")
        print("  HYDRA - Hyper-adaptive Dynamic Regime-switching Universal Agent")
        print("  ================================================================")
        print(f"  Trading: {trade_mode} | Sizing: {sizing_mode} | Kraken CLI v0.2.3 (WSL)")
        print(f"  {brain_status}")
        if self.paper:
            print("  Paper trading — no real money at risk.")
        else:
            print("  WARNING: Real trades with real money. Dead man's switch active.")
        print("")

    def _print_final_report(self):
        print(f"\n\n  {'='*80}")
        print(f"  HYDRA FINAL PERFORMANCE REPORT")
        print(f"  {'='*80}")

        for pair in self.pairs:
            engine = self.engines[pair]
            print(engine.get_performance_report())
            print()

        # Get final balance from exchange
        print("  FINAL EXCHANGE BALANCE:")
        print(f"  {'-'*40}")
        bal = KrakenCLI.balance()
        if "error" not in bal:
            for asset, amount in bal.items():
                print(f"    {asset}: {amount}")

        # Order journal
        if self.order_journal:
            print(f"\n  ORDER JOURNAL ({len(self.order_journal)} entries)")
            print(f"  {'-'*70}")
            for entry in self.order_journal[-20:]:
                lifecycle = entry.get("lifecycle") or {}
                state = lifecycle.get("state", "?")
                status_icon = "OK" if state == "FILLED" else ("~~" if state == "PARTIALLY_FILLED" else "XX")
                t_pair = entry.get("pair", "?")
                t_cur = "$" if t_pair.endswith("USDC") or t_pair.endswith("USD") else ""
                intent = entry.get("intent") or {}
                amount = intent.get("amount", 0)
                price = lifecycle.get("avg_fill_price") or intent.get("limit_price") or 0
                print(f"  [{status_icon}] {entry.get('placed_at','?')} | "
                      f"{entry.get('side','?'):<4} {amount:.8f} {t_pair:<10} "
                      f"@ {t_cur}{price:>10,.{4 if t_cur else 8}f} | {state}")
                if lifecycle.get("terminal_reason"):
                    print(f"        reason: {lifecycle['terminal_reason']}")
        else:
            print(f"\n  No orders placed during session.")

        # Export journal
        ts = int(time.time())
        base_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(base_dir, f"hydra_orders_{ts}.json")
        try:
            with open(log_file, "w") as f:
                json.dump(self.order_journal, f, indent=2)
            print(f"\n  Order journal exported to: {log_file}")
        except Exception as e:
            print(f"\n  [WARN] Could not export order journal: {e}")

        # Export competition results summary
        self._export_competition_results(base_dir, ts)

        print(f"\n  Past performance does not guarantee future results. Not financial advice.")
        print(f"  {'='*80}")

    def _compute_pair_realized_pnl(self, pair: str) -> float:
        """Compute realized P&L for a pair from the order journal.

        Uses average-cost-basis accounting: each sell's cost is valued at
        the running weighted-average buy price, so only *closed* round-trip
        profit/loss is reflected.  Unsold inventory cost stays out of
        realized P&L — it belongs in unrealized (pos_size * (price - avg_entry)).

        Only counts FILLED / PARTIALLY_FILLED entries — PLACED,
        PLACEMENT_FAILED, CANCELLED_UNFILLED, REJECTED are skipped.

        Accurate across resumes because it reads directly from on-disk
        journal state, not engine balances which get pooled and re-split.
        """
        FILL_STATES = ("FILLED", "PARTIALLY_FILLED")
        total_buy_cost = 0.0
        total_buy_vol = 0.0
        realized = 0.0
        for entry in self.order_journal:
            if entry.get("pair") != pair:
                continue
            lifecycle = entry.get("lifecycle") or {}
            if lifecycle.get("state") not in FILL_STATES:
                continue
            vol = lifecycle.get("vol_exec") or 0
            price = lifecycle.get("avg_fill_price")
            if price is None:
                # Legacy migrated entries that lack avg_fill_price fall
                # back to the placement intent. Post-PR entries always
                # carry avg_fill_price from the execution stream.
                intent = entry.get("intent") or {}
                price = intent.get("limit_price") or 0
            if vol <= 0 or price <= 0:
                continue
            side = entry.get("side")
            if side == "BUY":
                total_buy_cost += vol * price
                total_buy_vol += vol
            elif side == "SELL":
                avg_buy = (total_buy_cost / total_buy_vol) if total_buy_vol > 0 else 0
                cost_of_sold = vol * avg_buy
                realized += vol * price - cost_of_sold
                # Reduce the running buy pool by the sold quantity
                total_buy_cost = max(0.0, total_buy_cost - cost_of_sold)
                total_buy_vol = max(0.0, total_buy_vol - vol)
        return realized

    def _export_competition_results(self, base_dir: str, ts: int):
        """Export a competition_results.json for submission proof."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        pair_results = {}
        total_pnl_usd = 0.0
        total_trades = 0
        asset_prices = self._get_asset_prices()

        for pair in self.pairs:
            engine = self.engines[pair]
            price = engine.prices[-1] if engine.prices else 0
            quote = pair.split("/")[1]
            quote_usd = asset_prices.get(quote, 1.0)

            # Per-pair P&L from trade history (accurate across resumes).
            # Engine balances get pooled and re-split on each --resume, so
            # equity - initial_balance only reflects the current session.
            # Trade history gives the true per-pair realized performance.
            realized_pnl = self._compute_pair_realized_pnl(pair)
            unrealized_pnl = engine.position.size * (price - engine.position.avg_entry) if engine.position.size > 0 else 0
            pair_pnl = realized_pnl + unrealized_pnl
            pair_pnl_usd = pair_pnl * quote_usd
            total_pnl_usd += pair_pnl_usd
            total_trades += engine.total_trades
            win_rate = (engine.win_count / (engine.win_count + engine.loss_count) * 100) if (engine.win_count + engine.loss_count) > 0 else 0

            pair_results[pair] = {
                "realized_pnl": round(realized_pnl, 8),
                "unrealized_pnl": round(unrealized_pnl, 8),
                "net_pnl": round(pair_pnl, 8),
                "net_pnl_usd": round(pair_pnl_usd, 4),
                "max_drawdown_pct": round(engine.max_drawdown, 4),
                "total_trades": engine.total_trades,
                "wins": engine.win_count,
                "losses": engine.loss_count,
                "win_rate_pct": round(win_rate, 2),
                "sharpe": round(engine._calc_sharpe(), 4),
                "final_price": round(price, 8),
                "position_size": round(engine.position.size, 8),
            }

        # Aggregate cumulative P&L from competition start (survives --resume).
        start_balance = self._competition_start_balance if self._competition_start_balance is not None else self.initial_balance
        current_total_equity_usd = 0.0
        for pair in self.pairs:
            engine = self.engines[pair]
            price = engine.prices[-1] if engine.prices else 0
            equity = engine.balance + engine.position.size * price
            quote = pair.split("/")[1]
            quote_usd = asset_prices.get(quote, 1.0)
            current_total_equity_usd += equity * quote_usd
        cumulative_pnl_usd = current_total_equity_usd - start_balance

        results = {
            "agent": "HYDRA",
            "version": "2.13.3",
            "mode": self.mode,
            "paper": self.paper,
            "timestamp_start": datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat() if self.start_time else None,
            "timestamp_end": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(elapsed, 1),
            "pairs": self.pairs,
            "competition_start_balance": round(start_balance, 4),
            "current_total_equity": round(current_total_equity_usd, 4),
            "total_initial_balance": self.initial_balance,
            "total_net_pnl": round(cumulative_pnl_usd, 4),
            "total_pnl_pct": round((cumulative_pnl_usd / start_balance) * 100, 4) if start_balance > 0 else 0,
            "total_trades": total_trades,
            "pair_results": pair_results,
            "order_journal": self.order_journal,
        }

        results_file = os.path.join(base_dir, f"competition_results_{ts}.json")
        try:
            with open(results_file, "w") as f:
                json.dump(results, f, indent=2)
            print(f"  Competition results exported to: {results_file}")
        except Exception as e:
            print(f"  [WARN] Could not export competition results: {e}")


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

def main():
    parser = argparse.ArgumentParser(
        description="HYDRA — Live Regime-Adaptive Trading Agent for Kraken CLI",
    )
    parser.add_argument("--pairs", type=str, default="SOL/USDC,SOL/BTC,BTC/USDC",
                        help="Comma-separated trading pairs (default: SOL/USDC,SOL/BTC,BTC/USDC)")
    parser.add_argument("--balance", type=float, default=100.0,
                        help="Reference balance for position sizing (default: 100)")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between ticks (default: 300)")
    parser.add_argument("--candle-interval", type=int, default=15, choices=[1, 5, 15, 30, 60],
                        help="OHLC candle period in minutes (default: 15)")
    parser.add_argument("--duration", type=int, default=0,
                        help="Total duration in seconds (default: 0 = run forever, Ctrl+C to stop)")
    parser.add_argument("--ws-port", type=int, default=8765,
                        help="WebSocket port for dashboard (default: 8765)")
    parser.add_argument("--mode", type=str, default="conservative",
                        choices=["conservative", "competition"],
                        help="Sizing mode: conservative (quarter-Kelly) or competition (half-Kelly)")
    parser.add_argument("--paper", action="store_true",
                        help="Use paper trading (no API keys needed, no real money)")
    parser.add_argument("--reset-params", action="store_true",
                        help="Reset learned tuning parameters to defaults")
    parser.add_argument("--resume", action="store_true",
                        help="Resume from last session snapshot (engines + coordinator state)")

    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",")]
    candle_interval = args.candle_interval

    if args.interval is not None:
        tick_interval = args.interval
    else:
        tick_interval = 300

    if args.paper:
        print(f"\n  HYDRA — Paper trading mode. No real money at risk.")
    else:
        print(f"\n  WARNING: HYDRA will execute REAL trades on Kraken.")
    print(f"  Pairs: {', '.join(pairs)}")
    print(f"  Mode: {args.mode} | Balance ref: ${args.balance}")
    print(f"  Candles: {candle_interval}m | Tick: {tick_interval}s")
    print(f"  Duration: {args.duration}s")
    if not args.paper:
        print(f"  Dead man's switch will be active.")
    print()

    agent = HydraAgent(
        pairs=pairs,
        initial_balance=args.balance,
        interval_seconds=tick_interval,
        duration_seconds=args.duration,
        ws_port=args.ws_port,
        mode=args.mode,
        paper=args.paper,
        candle_interval=candle_interval,
        reset_params=args.reset_params,
        resume=args.resume,
    )
    agent.run()


if __name__ == "__main__":
    main()
