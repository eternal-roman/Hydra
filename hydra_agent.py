#!/usr/bin/env python3
"""
HYDRA Agent — Kraken CLI Integration Layer (Live Trading)

Connects the HYDRA engine to live Kraken market data via kraken-cli (WSL).
Supports live trading on SOL/USDC, SOL/BTC, and BTC/USDC.
Broadcasts state over WebSocket for the React dashboard.

Usage:
    python hydra_agent.py --pairs SOL/USDC,SOL/XBT --balance 100 --duration 600
    python hydra_agent.py --pairs SOL/USDC,SOL/XBT,XBT/USDC --interval 60
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

# Load .env file if present (no dependency needed)
_env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
if os.path.exists(_env_path):
    with open(_env_path) as _f:
        for _line in _f:
            _line = _line.strip()
            if _line and not _line.startswith("#") and "=" in _line:
                _k, _v = _line.split("=", 1)
                if _v and _k.strip() not in os.environ:
                    os.environ[_k.strip()] = _v.strip()

from hydra_engine import HydraEngine, CrossPairCoordinator, OrderBookAnalyzer, PositionSizer, SIZING_CONSERVATIVE, SIZING_COMPETITION
from hydra_tuner import ParameterTracker
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

    # Map friendly pair names to Kraken CLI pair names
    # The CLI uses different formats for different commands:
    # ticker/ohlc: "SOLXBT" (no slash) or "SOL/USDC" (with slash, depends on pair)
    PAIR_MAP = {
        "SOL/USDC": "SOL/USDC",
        "SOL/XBT": "SOLXBT",
        "SOL/BTC": "SOLXBT",
        "XBT/USDC": "XBTUSDC",
        "BTC/USDC": "XBTUSDC",
        "BTC/USD": "XBT/USD",
    }

    # Suffixes Kraken uses for non-tradable (staked/bonded/locked) assets
    STAKED_SUFFIXES = ('.B', '.S', '.M')

    # Kraken sometimes returns extended asset names — normalize to canonical form
    ASSET_NORMALIZE = {
        'XXBT': 'XBT', 'XBTC': 'XBT', 'BTC': 'XBT',
        'XETH': 'ETH', 'XSOL': 'SOL',
        'ZUSD': 'USD', 'ZUSDC': 'USDC',
    }

    # HF-001 fix: Kraken rejects orders whose price has more meaningful decimals
    # than the pair's native precision. Previously, f"{price:.8f}" was used
    # regardless of pair, which was safe only when the price came directly from
    # ticker["bid"]/ticker["ask"] (Kraken's own precision preserved through the
    # float round-trip). Any derived price (drift->amend, maker-fee shading,
    # midpoint) would hit "EOrder:Invalid price:PAIR price can only be specified
    # up to N decimals." Verified against Kraken's pairs endpoint.
    #
    # Entries are duplicated for every form Hydra may pass in: friendly
    # (SOL/USDC), slashless (SOLUSDC), and PAIR_MAP-resolved. _format_price
    # also has a slashless-match fallback in case new pairs get added without
    # updating every form.
    PRICE_DECIMALS = {
        'SOL/USDC': 2, 'SOLUSDC': 2,
        'XBT/USDC': 1, 'XBTUSDC': 1,
        'BTC/USDC': 1, 'BTCUSDC': 1,
        'SOL/XBT': 7, 'SOLXBT': 7,
        'SOL/BTC': 7, 'SOLBTC': 7,
        'BTC/USD': 1, 'BTCUSD': 1,
        'XBT/USD': 1, 'XBTUSD': 1,
    }
    PRICE_DECIMALS_DEFAULT = 8  # conservative fallback for unknown pairs

    @staticmethod
    def _is_staked(asset: str) -> bool:
        """Check if an asset name represents a staked/bonded/locked position."""
        return any(asset.endswith(s) for s in KrakenCLI.STAKED_SUFFIXES)

    @staticmethod
    def _normalize_asset(asset: str) -> str:
        """Normalize Kraken asset name to canonical form (e.g. XXBT → XBT).
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
        return KrakenCLI.PAIR_MAP.get(pair, pair)

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

    @staticmethod
    def depth(pair: str, count: int = 10) -> dict:
        """Fetch order book depth. Returns bids/asks arrays."""
        p = KrakenCLI._resolve_pair(pair)
        return KrakenCLI._run(["depth", p, "--count", str(count)])

    # ─── Private Account ───

    @staticmethod
    def balance() -> dict:
        """Get account balance. Returns {asset: amount} for non-zero balances."""
        data = KrakenCLI._run(["balance"])
        if isinstance(data, dict) and "error" not in data:
            return {k: float(v) for k, v in data.items() if float(v) > 0}
        return data

    @staticmethod
    def trade_balance() -> dict:
        """Get trade balance summary."""
        return KrakenCLI._run(["trade-balance"])

    @staticmethod
    def open_orders() -> dict:
        """Get open orders."""
        return KrakenCLI._run(["open-orders"])

    @staticmethod
    def trades_history() -> dict:
        """Get trade history."""
        return KrakenCLI._run(["trades-history"])

    @staticmethod
    def spreads(pair: str, since=None) -> dict:
        """Get recent bid/ask spreads for a pair.

        Returns raw Kraken response (dict with a data-bearing key plus a 'last' cursor),
        or {"error": ...} on failure. Callers should use the 'last' cursor to
        incrementally fetch only new rows on subsequent polls.
        """
        p = KrakenCLI._resolve_pair(pair)
        args = ["spreads", p]
        if since is not None:
            args.extend(["--since", str(since)])
        return KrakenCLI._run(args)

    @staticmethod
    def volume(pairs=None) -> dict:
        """Get 30-day trade volume and current fee tier.

        pairs: optional list of friendly pair symbols (e.g. ["SOL/USDC","XBT/USDC"])
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
    def order_amend(txid, limit_price=None, order_qty=None,
                    post_only: bool = True, pair: str = None) -> dict:
        """Amend a live limit order in place (preserves queue priority and txid).

        At least one of limit_price / order_qty must be provided, and txid must
        be non-empty. post_only rejects the amend if the new price would cross
        the book — safer than a silent taker flip.

        HF-001: pair is an optional arg used ONLY for price precision rounding
        via PRICE_DECIMALS. If omitted, falls back to 8 decimals (unsafe for
        low-precision pairs like SOL/USDC which accept only 2). Any caller that
        computes a derived limit_price MUST pass pair to get correct rounding.
        """
        if txid is None or txid == "":
            return {"error": "order_amend requires txid"}
        if limit_price is None and order_qty is None:
            return {"error": "order_amend requires limit_price or order_qty"}
        args = ["order", "amend", "--txid", str(txid)]
        if limit_price is not None:
            if pair:
                args.extend(["--limit-price", KrakenCLI._format_price(pair, limit_price)])
            else:
                args.extend(["--limit-price", f"{float(limit_price):.8f}"])
        if order_qty is not None:
            args.extend(["--order-qty", f"{float(order_qty):.8f}"])
        if post_only:
            args.append("--post-only")
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

    @staticmethod
    def paper_positions() -> dict:
        """Get paper trading positions."""
        return KrakenCLI._run(["paper", "positions"])


# ═══════════════════════════════════════════════════════════════
# WEBSOCKET BROADCAST SERVER (for React Dashboard)
# ═══════════════════════════════════════════════════════════════

class DashboardBroadcaster:
    """Async WebSocket server that broadcasts agent state to dashboard clients."""

    def __init__(self, host: str = "0.0.0.0", port: int = 8765):
        self.host = host
        self.port = port
        self.clients = set()
        self.latest_state = {}
        self._loop = None
        self._thread = None

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
            # Send latest state immediately on connect
            if self.latest_state:
                await websocket.send(json.dumps(self.latest_state))
            async for msg in websocket:
                pass  # We only broadcast, don't receive
        except Exception:
            pass
        finally:
            self.clients.discard(websocket)
            print(f"  [WS] Dashboard client disconnected ({len(self.clients)} total)")

    def broadcast(self, state: dict):
        """Broadcast state to all connected dashboard clients."""
        self.latest_state = state
        if self._loop and self.clients:
            msg = json.dumps(state)
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
# EXECUTION STREAM — kraken ws executions push reconciler
# ═══════════════════════════════════════════════════════════════

class ExecutionStream:
    """Consumes `kraken ws executions` and delivers push-based lifecycle
    events to the agent tick loop.

    Architecture:
        start()  - spawn the CLI subprocess (wsl -d Ubuntu -- bash -c "...")
                   and a daemon reader thread that parses stdout line-by-line
                   into an internal queue.
        register(order_id, userref, journal_index, pair, side,
                 placed_amount, engine_ref, pre_trade_snapshot)
                 - correlate a freshly placed order with its in-memory
                   journal entry and engine rollback handle.
        drain_events() -> List[dict]
                 - called every tick by the agent. Pops queued WS events,
                   aggregates fills by order_id, emits one terminal event
                   per order (FILLED / PARTIALLY_FILLED / CANCELLED_UNFILLED
                   / REJECTED), hands them to the caller.
        stop()   - terminate subprocess, join reader and stderr threads.
        healthy  - True iff subprocess + reader alive AND a heartbeat was
                   seen within HEARTBEAT_TIMEOUT_S.
        health_status() -> (ok, reason)
                 - same check, returning the diagnostic reason string so
                   the agent's tick warning identifies WHICH check failed.
        ensure_healthy() -> (ok, reason)
                 - called every tick by the agent. If unhealthy, attempts
                   one restart of the subprocess subject to RESTART_COOLDOWN_S
                   so we don't thrash on a persistently broken environment.

    Correlation keys: order_id (from REST placement response) is primary;
    order_userref (numeric tag we passed on placement) is fallback. Both
    are checked — whichever arrives first resolves the match.

    Numeric fields on incoming WS events are real JSON floats/ints in CLI
    v0.2.3 (confirmed by spike on 2026-04-11) — no string coercion needed.

    Paper mode uses paper=True which short-circuits start() and lets the
    place_order helper emit synthetic terminal events directly into the
    event queue. No subprocess is spawned.
    """

    # 30s heartbeat threshold gives slow WSL cold-starts (and the few seconds
    # kraken takes to authenticate + subscribe to the WS channel) headroom
    # before we declare the stream stale. Real heartbeats arrive ~1/sec, so
    # any healthy stream is well under this bound.
    HEARTBEAT_TIMEOUT_S = 30.0
    READER_JOIN_TIMEOUT_S = 5.0
    # Minimum seconds between auto-restart attempts so we don't thrash when
    # WSL or the network is genuinely down.
    RESTART_COOLDOWN_S = 30.0

    def __init__(self, paper: bool = False):
        self.paper = paper
        self._proc: Optional[subprocess.Popen] = None
        self._reader_thread: Optional[threading.Thread] = None
        self._stderr_thread: Optional[threading.Thread] = None
        self._event_queue: "queue.Queue[tuple]" = queue.Queue()
        # Correlation maps — primary by order_id, secondary by userref
        self._known_orders: Dict[str, dict] = {}
        self._userref_to_order_id: Dict[int, str] = {}
        self._last_heartbeat: float = 0.0
        self._last_sequence: Optional[int] = None
        self._lock = threading.Lock()
        self._shutdown = threading.Event()
        # Diagnostic state for health_status() and auto-restart bookkeeping.
        self._reader_exit_reason: Optional[str] = None
        self._last_restart_attempt: float = 0.0
        self._restart_count: int = 0

    # ───────── lifecycle ─────────

    def start(self) -> bool:
        """Spawn the subprocess and reader thread. Returns True on success.

        Paper mode is a no-op (returns True). In live mode, if spawning or
        reader-start fails, returns False and leaves self.healthy==False —
        the caller should log and proceed (placements still work, the
        journal just won't get terminal events until the stream recovers).
        """
        if self.paper:
            self._last_heartbeat = time.time()
            return True
        # Reset state in case start() is called as part of a restart.
        self._shutdown.clear()
        self._reader_exit_reason = None
        cmd = [
            "wsl", "-d", "Ubuntu", "--", "bash", "-c",
            "source ~/.cargo/env && kraken ws executions -o json "
            "--snap-orders true --snap-trades true",
        ]
        try:
            self._proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                bufsize=1, text=True,
            )
        except Exception as e:
            print(f"  [EXECSTREAM] failed to spawn subprocess: {type(e).__name__}: {e}")
            return False
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="ExecutionStream-reader", daemon=True,
        )
        self._reader_thread.start()
        # stderr drain thread — without this, anything kraken writes to stderr
        # accumulates in the OS pipe buffer (~64 KB on Linux) and eventually
        # blocks the subprocess on write, which silently freezes the stream.
        # Draining also gives us visible kraken-side error messages.
        self._stderr_thread = threading.Thread(
            target=self._stderr_loop, name="ExecutionStream-stderr", daemon=True,
        )
        self._stderr_thread.start()
        self._last_heartbeat = time.time()
        print("  [EXECSTREAM] kraken ws executions stream started")
        return True

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
        """Return (healthy, reason). When healthy, reason is the empty string.
        When unhealthy, reason describes WHICH check failed so the warning
        printed at the agent tick body is actionable rather than generic."""
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
        age = time.time() - self._last_heartbeat
        if age > self.HEARTBEAT_TIMEOUT_S:
            return False, (
                f"no heartbeat for {age:.0f}s "
                f"(threshold {self.HEARTBEAT_TIMEOUT_S:.0f}s)"
            )
        return True, ""

    def ensure_healthy(self) -> Tuple[bool, str]:
        """Check health and, if unhealthy, attempt one restart subject to a
        cooldown so we don't thrash. Returns the post-action (healthy, reason)
        tuple — i.e. the state the caller should report to the user."""
        if self.paper:
            return True, ""
        healthy, reason = self.health_status()
        if healthy:
            return True, ""
        now = time.time()
        if now - self._last_restart_attempt < self.RESTART_COOLDOWN_S:
            return healthy, reason
        self._last_restart_attempt = now
        self._restart_count += 1
        print(f"  [EXECSTREAM] auto-restart #{self._restart_count}: {reason}")
        try:
            self.stop()
        except Exception as e:
            print(f"  [EXECSTREAM] stop during restart failed: {type(e).__name__}: {e}")
        if not self.start():
            return False, "restart spawn failed"
        return self.health_status()

    # ───────── reader thread ─────────

    def _reader_loop(self) -> None:
        """Daemon thread body. Line-iterates subprocess stdout, parses each
        line as JSON, dispatches to the internal queue. Records exit reason
        in self._reader_exit_reason so health_status() can report it."""
        assert self._proc is not None
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
                    # CLI diagnostic noise — log but keep reading.
                    print(f"  [EXECSTREAM] non-JSON line: {line[:120]}")
                    continue
                self._dispatch(msg)
        except Exception as e:
            exit_reason = f"crashed: {type(e).__name__}: {e}"
            if not self._shutdown.is_set():
                print(f"  [EXECSTREAM] reader thread error: {type(e).__name__}: {e}")
        finally:
            self._reader_exit_reason = exit_reason
            if not self._shutdown.is_set():
                print(f"  [EXECSTREAM] reader thread exited: {exit_reason}")

    def _stderr_loop(self) -> None:
        """Daemon thread body for stderr. Drains and prints anything kraken
        emits to stderr. Without this, the OS pipe buffer fills up and blocks
        the subprocess on its next stderr write — which silently freezes
        stdout (and therefore the heartbeat) too. Also gives us visibility
        into kraken-side connection errors."""
        if self._proc is None or self._proc.stderr is None:
            return
        try:
            for raw in self._proc.stderr:  # type: ignore[union-attr]
                if self._shutdown.is_set():
                    break
                line = raw.rstrip()
                if line:
                    print(f"  [EXECSTREAM stderr] {line[:200]}")
        except Exception as e:
            if not self._shutdown.is_set():
                print(f"  [EXECSTREAM] stderr reader error: {type(e).__name__}: {e}")

    def _dispatch(self, msg: Dict[str, Any]) -> None:
        """Classify an incoming WS message and enqueue data events."""
        channel = msg.get("channel")
        if channel == "heartbeat":
            self._last_heartbeat = time.time()
            return
        if channel == "status":
            # Connection status update; informational only
            return
        if msg.get("method") == "subscribe":
            if not msg.get("success"):
                print(f"  [EXECSTREAM] subscribe failed: {msg}")
            return
        if channel != "executions":
            return
        # Bump the heartbeat on any executions traffic — it's a liveness signal
        self._last_heartbeat = time.time()
        # Sequence gap detection (warn only — subsequent snapshot recovers)
        seq = msg.get("sequence")
        if isinstance(seq, int):
            if self._last_sequence is not None and seq != self._last_sequence + 1:
                print(
                    f"  [EXECSTREAM] sequence gap {self._last_sequence}->{seq} "
                    f"(executions may have been dropped; waiting for next snapshot)"
                )
            self._last_sequence = seq
        msg_type = msg.get("type")  # "snapshot" | "update"
        data = msg.get("data") or []
        if not isinstance(data, list):
            return
        for entry in data:
            if isinstance(entry, dict):
                self._event_queue.put((msg_type or "update", entry))

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
            eps = max(1e-9, placed * 1e-6)
            avg_price = (known["cost_running"] / vol_exec) if vol_exec > 0 else None

            if order_status == "filled":
                if abs(vol_exec - placed) <= eps:
                    state = "FILLED"
                else:
                    state = "PARTIALLY_FILLED"
                terminal_reason: Optional[str] = None
            elif order_status in ("canceled", "expired"):
                reason = entry.get("reason") or order_status
                terminal_reason = str(reason)
                if vol_exec <= eps:
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
        self._last_heartbeat = time.time()

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
    CROSS_PAIR = "SOL/XBT"          # Opportunistic regime-driven swaps
    BTC_PAIR = "XBT/USDC"           # For BTC/USDC when we can afford it
    ORDER_JOURNAL_CAP = 2000        # Bound in-memory order journal
    SNAPSHOT_EVERY_N_TICKS = 12     # ~1h at 5-min candles

    def __init__(
        self,
        pairs: List[str],
        initial_balance: float = 100.0,
        interval_seconds: int = 60,
        duration_seconds: int = 600,
        ws_port: int = 8765,
        mode: str = "conservative",
        paper: bool = False,
        candle_interval: int = 5,
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
        self._kraken_lock = threading.Lock()  # Serialize Kraken API calls across threads
        self._completed_trades_since_update = 0  # Counter for tuner update cadence
        # Monotonic client tag seeded from wall-clock to avoid collisions
        # across restarts; flows into Kraken as --userref and comes back on
        # the WS executions stream as order_userref for correlation.
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
            # Scale regime thresholds for candle interval (tuned for 5-min)
            vol_atr = 3.0 if candle_interval >= 5 else 4.0
            vol_bb = 0.06 if candle_interval >= 5 else 0.08
            self.engines[pair] = HydraEngine(
                initial_balance=initial_balance / len(pairs),
                asset=pair,
                sizing=sizing,
                candle_interval=candle_interval,
                volatile_atr_pct=vol_atr,
                volatile_bb_width=vol_bb,
            )
            # Apply any previously learned tuned params
            tuned = self.trackers[pair].get_tunable_params()
            self.engines[pair].apply_tuned_params(tuned)
            if self.trackers[pair].update_count > 0:
                print(f"  [TUNER] {pair}: loaded tuned params (update #{self.trackers[pair].update_count})")

        # Dashboard broadcaster
        self.broadcaster = DashboardBroadcaster(port=ws_port)

        # AI Brain (optional — Claude for analysis, Grok for strategic depth)
        self.brain = None
        if HAS_BRAIN:
            anthropic_key = os.environ.get("ANTHROPIC_API_KEY", "")
            openai_key = os.environ.get("OPENAI_API_KEY", "")
            xai_key = os.environ.get("XAI_API_KEY", "")
            if anthropic_key or openai_key or xai_key:
                try:
                    strategist_threshold = 0.50 if self.mode == "competition" else 0.65
                    self.brain = HydraBrain(
                        anthropic_key=anthropic_key, openai_key=openai_key,
                        xai_key=xai_key, strategist_threshold=strategist_threshold,
                    )
                except Exception as e:
                    print(f"  [WARN] Brain init failed: {e}")

        # Cross-pair regime coordinator
        self.coordinator = CrossPairCoordinator(pairs)
        self._swap_counter = 0  # Monotonic swap ID generator

        # Execution stream — push-based reconciler backed by `kraken ws
        # executions`. Paper mode short-circuits the subprocess and uses
        # synthetic fill events (inject_event) so the same code path
        # handles both real and paper flows.
        self.execution_stream = ExecutionStream(paper=paper)
        # Tracks the most recently logged unhealthy reason so the tick body
        # only prints on transitions instead of spamming the warning every
        # tick. None means "currently healthy or never warned".
        self._exec_stream_warned_reason: Optional[str] = None

        # Fee tier cache — refreshed at most once per hour from `kraken volume`.
        # Shape: {"volume_30d_usd": float|None, "pair_fees": {pair: {"maker_pct","taker_pct"}}}
        self._fee_tier_cache: dict = {}
        self._fee_tier_fetched_at: float = 0.0

        # Spread history — diagnostic rolling window per pair, polled every 5 ticks.
        # Not persisted in snapshot; re-fills cheaply on restart.
        self._spread_history: Dict[str, list] = {pair: [] for pair in pairs}
        self._spread_last_cursor: Dict[str, object] = {pair: None for pair in pairs}

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
        # Tear down the execution stream subprocess + reader thread
        try:
            self.execution_stream.stop()
        except Exception as e:
            print(f"  [HYDRA] ExecutionStream stop failed: {e}")
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
        }
        path = self._snapshot_path()
        tmp = path + ".tmp"
        try:
            with open(tmp, "w") as f:
                json.dump(snapshot, f, default=str)
            os.replace(tmp, path)
        except Exception as e:
            print(f"  [SNAPSHOT] Save failed: {e}")

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
            for pair, eng_snap in snapshot.get("engines", {}).items():
                if pair in self.engines:
                    self.engines[pair].restore_runtime(eng_snap)
            for pair, history in snapshot.get("coordinator_regime_history", {}).items():
                if pair in self.coordinator.regime_history:
                    self.coordinator.regime_history[pair] = list(history)
            self.order_journal = list(snapshot.get("order_journal", []))
            if snapshot.get("competition_start_balance") is not None:
                self._competition_start_balance = float(snapshot["competition_start_balance"])
            print(f"  [SNAPSHOT] Restored session from {snapshot.get('timestamp', '?')}")
        except Exception as e:
            print(f"  [SNAPSHOT] Load failed: {e}, starting fresh.")

    def _merge_order_journal(self):
        """Merge the on-disk hydra_order_journal.json into self.order_journal.

        Rationale: _save_snapshot caps order_journal at [-200:] for
        compactness, so _load_snapshot can only ever restore the last 200
        entries. The rolling file (hydra_order_journal.json) is the
        authoritative long-horizon record — a prior bug would overwrite it
        on the first tick after a restart, destroying any history older
        than the in-memory journal. This method loads the rolling file at
        startup and unions it with whatever was restored from the snapshot
        so restart never truncates historical depth. Bounded by
        ORDER_JOURNAL_CAP.

        Dedup key is (placed_at, order_id) when a Kraken order_id is
        available, else (placed_at, pair, side, intent.amount) — precise
        enough because placed_at has microsecond resolution.

        Conflict policy: on duplicate key, the ROLLING FILE wins. Both
        files are saved from the same in-memory journal during normal
        operation, so divergence only happens via manual data repair or
        external tooling — and in those cases the rolling file is what
        gets edited (see PR #40 data repair). After the merge, the next
        _save_snapshot rewrites the snapshot to match.
        """
        rolling_file = os.path.join(self._snapshot_dir, "hydra_order_journal.json")
        if not os.path.exists(rolling_file):
            return
        try:
            with open(rolling_file, "r") as f:
                on_disk = json.load(f)
        except Exception as e:
            print(f"  [JOURNAL] Could not read rolling file for merge: {e}")
            return
        if not isinstance(on_disk, list):
            return

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
        for e in on_disk:
            k = _key(e)
            if k not in seen:
                seen[k] = e
                merged_count += 1
            else:
                # Rolling file wins on conflict — see docstring.
                if seen[k] is not e:
                    seen[k] = e
                    overwritten_count += 1

        merged = sorted(seen.values(), key=lambda e: e.get("placed_at", ""))
        if len(merged) > self.ORDER_JOURNAL_CAP:
            merged = merged[-self.ORDER_JOURNAL_CAP:]
        self.order_journal = merged
        if merged_count or overwritten_count:
            parts = []
            if merged_count:
                parts.append(f"merged {merged_count} new")
            if overwritten_count:
                parts.append(f"overwrote {overwritten_count} stale")
            print(f"  [JOURNAL] {' + '.join(parts)} from "
                  f"{os.path.basename(rolling_file)}; total = {len(self.order_journal)}")

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
            self._cached_balance = bal
        else:
            print(f"  [WARN] Balance check failed: {bal} — using --balance fallback: ${self.initial_balance:,.2f}")

        # Convert engine balances from USD to quote currency for non-USD pairs
        # (e.g. SOL/XBT engine needs balance in XBT, not USD).
        # Skip if _set_engine_balances was already called above (live mode with
        # exchange data).  Resumed sessions still need conversion because old
        # snapshots (pre-multi-currency fix) stored USD values for XBT-quoted pairs.
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

                ts = datetime.now(timezone.utc).strftime("%H:%M:%S UTC")
                print(f"\n  === Tick {tick} | {ts} | Elapsed: {elapsed:.0f}s | Remaining: {remaining} ===")

                # Refresh dead man's switch every tick (live mode only)
                if not self.paper:
                    KrakenCLI.cancel_after(self._dms_timeout)
                    time.sleep(2)  # Rate limit

                # Phase 1: Fetch data and run all engines (regimes, signals, positions)
                engine_states = {}
                for pair in self.pairs:
                    engine_states[pair] = self._fetch_and_tick(pair)
                    time.sleep(2)  # Rate limit after OHLC fetch

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

                cross_overrides = self.coordinator.get_overrides(engine_states)
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
                # Fetch depth data and apply confidence modifiers before brain runs
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if not state:
                        continue
                    time.sleep(2)  # Rate limit
                    depth = KrakenCLI.depth(pair, count=10)
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

                # Phase 1.8: Spread history diagnostic (polled every 5 ticks, attached every tick)
                # Purely observational — does NOT influence signal confidence or sizing.
                if tick % 5 == 0:
                    for pair in self.pairs:
                        time.sleep(2)  # Rate limit
                        sp = KrakenCLI.spreads(pair, since=self._spread_last_cursor.get(pair))
                        self._record_spreads(pair, sp)
                # Attach the latest 60-row cached window to each pair's state every tick
                # (engine.tick() rebuilds state dicts, so we must re-attach even on non-poll ticks)
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if state is not None:
                        state["spread_history"] = list(self._spread_history.get(pair, []))[-60:]

                # ── Total modifier cap ──────────────────────────────────
                # External modifiers (cross-pair + order book) can reduce confidence without limit
                # but cannot boost it more than +0.15 above the engine's original signal.
                # This prevents stacking modifiers from inflating weak signals into high-conviction
                # trades that get oversized via Kelly criterion.
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

                # Phase 2: Run brain with full cross-pair context (parallel across pairs)
                all_states = {}
                brain_pairs = []
                for pair in self.pairs:
                    state = engine_states.get(pair)
                    if state:
                        if state["signal"]["action"] != "HOLD" and self.brain:
                            brain_pairs.append((pair, state))
                        else:
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
                        engine = self.engines[pair]
                        pre_trade_snap = engine.snapshot_position()
                        trade = engine.execute_signal(
                            action=sig.get("action", "HOLD"),
                            confidence=sig.get("confidence", 0),
                            reason=sig.get("reason", ""),
                            strategy=state.get("strategy", "MOMENTUM"),
                        )
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

        When a brain is active, uses generate_only=True so the engine produces
        signals without executing trades internally. This prevents engine state
        from diverging when the brain later overrides a signal.
        """
        engine = self.engines[pair]

        # Fetch latest candle
        candles = KrakenCLI.ohlc(pair, interval=self.candle_interval)
        if candles:
            engine.ingest_candle(candles[-1])
        else:
            # Fallback to ticker — assign a synthetic timestamp aligned to the
            # candle interval so the dedup logic in ingest_candle works correctly.
            # Without this, repeated ticker-fallback candles each get a unique
            # time.time() and silently inflate the candle history.
            ticker = KrakenCLI.ticker(pair)
            if "price" in ticker:
                p = ticker["price"]
                interval_secs = self.candle_interval * 60
                aligned_ts = (int(time.time()) // interval_secs) * interval_secs
                engine.ingest_candle({
                    "open": p, "high": p, "low": p, "close": p, "volume": 0,
                    "timestamp": aligned_ts,
                })

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
            return state

        # Pre-brain filter: skip brain for BUY signals that can't produce tradeable order size
        if state["signal"]["action"] == "BUY":
            engine = self.engines[pair]
            test_size = engine.sizer.calculate(
                state["signal"]["confidence"], engine.balance, state["price"], pair,
            )
            if test_size == 0:
                return state  # Signal too weak to trade; don't waste brain tokens

        # Inject cross-pair triangle context before deliberation
        state["triangle_context"] = self._build_triangle_context(pair, all_engine_states)

        # Fetch spread data for risk assessment (serialized to respect Kraken rate limits)
        try:
            with self._kraken_lock:
                time.sleep(2)  # Rate limit
                ticker = KrakenCLI.ticker(pair)
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
            }
            # Apply AI decision to engine state
            # Note: engine ran with generate_only=True, so no trade was executed yet.
            # Modifying the signal here changes what execute_signal() will act on.
            if decision.action == "OVERRIDE":
                state["signal"]["action"] = decision.final_signal
                state["signal"]["confidence"] = decision.confidence_adj
                state["signal"]["reason"] = f"[AI OVERRIDE] {decision.combined_summary}"
            elif decision.action == "ADJUST":
                state["signal"]["confidence"] = decision.confidence_adj
                state["signal"]["reason"] = f"[AI ADJUSTED] {decision.combined_summary}"
            # CONFIRM leaves signal unchanged, just adds reasoning
        except Exception as e:
            state["ai_decision"] = {"action": "FALLBACK", "error": str(e), "fallback": True}

        return state

    def _build_triangle_context(self, current_pair: str, all_states: dict) -> dict:
        """Build cross-pair context summary for brain deliberation."""
        pairs = {}
        sol_exposure = 0.0
        xbt_exposure = 0.0

        for pair, state in all_states.items():
            if state is None:
                continue
            pos = state.get("position", {}).get("size", 0)
            price = state.get("price", 0)

            # Net asset exposure across the triangle
            if pair == "SOL/USDC":
                sol_exposure += pos
            elif pair == "SOL/XBT":
                sol_exposure += pos
                xbt_exposure -= pos * price  # long SOL/XBT = short XBT
            elif pair == "XBT/USDC":
                xbt_exposure += pos

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
                "XBT": round(xbt_exposure, 6),
            },
        }

    # ─── Order placement (writes the journal, registers with the stream) ───

    def _next_userref(self) -> int:
        """Monotonic client tag used for --userref on placement so WS
        executions can correlate back to the local journal entry."""
        self._userref_counter += 1
        # Kraken userref is int32. Wrap defensively.
        if self._userref_counter > 0x7FFFFFFF:
            self._userref_counter = int(time.time()) & 0x7FFFFFFF
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
                "book_confidence_modifier": book_mod,
                "brain_verdict": brain_verdict,
                "swap_id": trade.get("swap_id"),
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

        # ─── Ticker fetch ───
        time.sleep(2)
        ticker = KrakenCLI.ticker(pair)
        if "error" in ticker or "bid" not in ticker:
            print(f"  [TRADE] Cannot fetch ticker for {pair}, skipping")
            self._finalize_failed_entry(
                entry, terminal_reason=f"ticker_failed:{ticker.get('error', 'no bid/ask')}",
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
        time.sleep(2)
        fresh_ticker = KrakenCLI.ticker(pair)
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

    def _apply_execution_event(self, event: Dict[str, Any]) -> None:
        """Apply one terminal event from the execution stream to the
        journal entry it came from AND the engine state. Called in the
        tick loop after drain_events()."""
        idx = event.get("journal_index")
        if not isinstance(idx, int) or idx < 0 or idx >= len(self.order_journal):
            print(f"  [EXEC] stale journal_index {idx} — event dropped")
            return
        entry = self.order_journal[idx]
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
            # actual fill was only vol_exec. HydraEngine does not yet
            # expose a partial-adjust primitive, so v1 leaves the engine
            # over-committed (erring toward not re-placing on top) and the
            # journal carries the true vol_exec for correct P&L. Logged
            # loudly so the operator sees any divergence. Follow-up:
            # HydraEngine.adjust_position(target_size) for exact handling.
            ratio = (vol_exec / placed_amount) if placed_amount > 0 else 0.0
            print(f"  [EXEC] {pair} {side} PARTIALLY_FILLED: "
                  f"filled {vol_exec:.8f}/{placed_amount:.8f} ({ratio:.1%}) — "
                  f"engine over-committed; journal has correct vol_exec")
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
        Executes the sell leg first, then the buy leg.
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

        # Leg 2: Buy on the target pair
        # Use the proceeds to size the buy
        buy_price = buy_state.get("price", 0)
        if buy_price <= 0:
            print(f"  [SWAP] Cannot execute buy leg: no price for {buy_pair}")
            return

        buy_engine = self.engines.get(buy_pair)
        if not buy_engine:
            print(f"  [SWAP] No engine for {buy_pair}, skipping buy leg")
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
            print(f"  [SWAP] Engine rejected buy on {buy_pair} (halted or insufficient balance), skipping buy leg")
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
                    if "SOL/XBT" in all_states:
                        xbt_regime = all_states["SOL/XBT"].get("regime")
                        if xbt_regime in ("TREND_UP", "RANGING"):
                            print(f"  [REGIME] Cross-pair opportunity: SOL weakening vs USDC but "
                                  f"SOL/XBT is {xbt_regime} — consider selling SOL for BTC")

                # If SOL/USDC shifts to TREND_UP, consider buying SOL with USDC
                if pair == "SOL/USDC" and current_regime == "TREND_UP":
                    print(f"  [REGIME] SOL trending up — MOMENTUM strategy active")

            self.prev_regimes[pair] = current_regime

    def _set_engine_balances(self, per_pair_usd: float):
        """Set engine balances, converting USD to quote currency for non-USD pairs.

        The engine's internal bookkeeping (balance -= cost) uses the quote currency,
        so SOL/XBT must have its balance in XBT, not USD. Without this conversion,
        position sizes for XBT-quoted pairs are wildly inflated (dividing USD by an
        XBT-denominated price).

        When an engine already holds a position (e.g. from --resume), we set
        initial_balance = cash + position_value so that P&L starts at 0% from
        the point of the balance reset, rather than showing a bogus gain from
        the position being valued against a tiny converted initial balance.
        """
        prices = self._get_asset_prices()
        for pair in self.pairs:
            engine = self.engines[pair]
            quote = pair.split("/")[1]
            if quote not in ("USDC", "USD") and quote in prices and prices[quote] > 0:
                balance_quote = per_pair_usd / prices[quote]
                engine.balance = balance_quote
                # Account for existing position value so P&L doesn't spike
                current_price = engine.prices[-1] if engine.prices else 0
                equity = balance_quote + engine.position.size * current_price
                engine.initial_balance = equity
                engine.peak_equity = equity
                print(f"  [HYDRA] {pair}: balance converted ${per_pair_usd:,.2f} -> {balance_quote:.8f} {quote} (equity {equity:.8f})")
            else:
                current_price = engine.prices[-1] if engine.prices else 0
                equity = per_pair_usd + engine.position.size * current_price
                engine.balance = per_pair_usd
                engine.initial_balance = equity
                engine.peak_equity = equity

    def _get_asset_prices(self) -> dict:
        """Get current USD prices for known assets from engine state.
        Returns {canonical_asset: usd_price}."""
        prices = {"USDC": 1.0, "USD": 1.0}
        for pair, engine in self.engines.items():
            if engine.prices:
                base, quote = pair.split("/")
                if quote in ("USDC", "USD"):
                    prices[base] = engine.prices[-1]
        # Derive XBT price from SOL/XBT if XBT/USDC not available
        if "XBT" not in prices and "SOL" in prices:
            sol_xbt_engine = self.engines.get("SOL/XBT")
            if sol_xbt_engine and sol_xbt_engine.prices:
                sol_per_xbt = sol_xbt_engine.prices[-1]
                if sol_per_xbt > 0:
                    prices["XBT"] = prices["SOL"] / sol_per_xbt
        return prices

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
        # Kraken may return fee keys in several forms ("SOLUSDC", "SOL/USDC", "XBTUSDC",
        # "XXBTZUSD" historically). Build a forgiving reverse map that accepts both the
        # PAIR_MAP-resolved form and the slashless form of the original friendly pair.
        pair_reverse = {}
        for p in getattr(self, "pairs", []):
            resolved = KrakenCLI._resolve_pair(p)
            pair_reverse[resolved] = p
            pair_reverse[p.replace("/", "")] = p  # slashless fallback ("SOLUSDC" → "SOL/USDC")
            pair_reverse[p] = p                   # passthrough
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

    def _record_spreads(self, pair: str, response: dict) -> None:
        """Append new rows from a `kraken spreads` response into the rolling history.

        Updates the 'last' cursor so subsequent polls incrementally fetch only new rows.
        Silently drops malformed rows and caps the per-pair history at 120 entries.
        No-ops on error responses (diagnostic, not safety-critical).
        """
        if not isinstance(response, dict) or "error" in response:
            return
        rows = []
        for key, val in response.items():
            if key == "last":
                try:
                    self._spread_last_cursor[pair] = int(val)
                except (TypeError, ValueError):
                    pass
                continue
            if isinstance(val, list) and not rows:
                rows = val
        history = self._spread_history.setdefault(pair, [])
        for row in rows:
            if not isinstance(row, list) or len(row) < 3:
                continue
            try:
                ts = float(row[0])
                bid = float(row[1])
                ask = float(row[2])
            except (TypeError, ValueError):
                continue
            mid = (bid + ask) / 2.0 if (bid > 0 and ask > 0) else 0.0
            spread_bps = round((ask - bid) / mid * 10000, 2) if mid > 0 else 0.0
            history.append({"ts": ts, "bid": bid, "ask": ask, "spread_bps": spread_bps})
        if len(history) > 120:
            self._spread_history[pair] = history[-120:]

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
        # Fetch balance every 5th tick to reduce API calls
        if tick % 5 == 1 or not hasattr(self, '_cached_balance'):
            bal = KrakenCLI.paper_balance() if self.paper else KrakenCLI.balance()
            self._cached_balance = bal if "error" not in bal else getattr(self, '_cached_balance', {})
            time.sleep(2)  # Rate limit
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

        signal_icon = {"BUY": "^", "SELL": "v", "HOLD": "-"}.get(s["action"], "?")

        print(f"  | {pair:<10} | {cur}{state['price']:>10,.4f} | "
              f"{state['regime']:<10} -> {state['strategy']:<15} | "
              f"{signal_icon} {s['action']:<4} ({s['confidence']:.2f}) | "
              f"Eq: {cur}{p['equity']:>10,.{2 if is_usd else 8}f} | "
              f"P&L: {p['pnl_pct']:>+.2f}% | DD: {p['max_drawdown_pct']:.1f}%")

        if pos["size"] > 0:
            print(f"  |            | Pos: {pos['size']:.8f} @ {cur}{pos['avg_entry']:,.4f} | "
                  f"Unrealized: {cur}{pos['unrealized_pnl']:>+,.{2 if is_usd else 8}f}")

        if state.get("ai_decision") and not state["ai_decision"].get("fallback"):
            ai = state["ai_decision"]
            print(f"  |  [AI] {ai['action']} → {ai['final_signal']} | {ai.get('summary', '')[:70]}")

        if state.get("last_trade"):
            t = state["last_trade"]
            profit_str = f" | Profit: ${t['profit']:+,.2f}" if t.get("profit") is not None else ""
            print(f"  |  >>> SIGNAL: {t['action']} {t['amount']:.8f} @ ${t['price']:,.4f}{profit_str}")
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

        Sums sell revenue minus buy cost from every FILLED and
        PARTIALLY_FILLED entry for the pair, using lifecycle.vol_exec and
        lifecycle.avg_fill_price (the execution-stream truth, not the
        bot's placement intent). Counts only actual fills — PLACED,
        PLACEMENT_FAILED, CANCELLED_UNFILLED, REJECTED entries are
        skipped because they never produced exchange-side quantity.

        Accurate across resumes because it reads directly from on-disk
        journal state, not engine balances which get pooled and re-split.
        """
        FILL_STATES = ("FILLED", "PARTIALLY_FILLED")
        buy_cost = 0.0
        sell_revenue = 0.0
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
                buy_cost += vol * price
            elif side == "SELL":
                sell_revenue += vol * price
        return sell_revenue - buy_cost

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
            "version": "1.1.0",
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
    parser.add_argument("--pairs", type=str, default="SOL/USDC,SOL/XBT,XBT/USDC",
                        help="Comma-separated trading pairs (default: SOL/USDC,SOL/XBT,XBT/USDC)")
    parser.add_argument("--balance", type=float, default=100.0,
                        help="Reference balance for position sizing (default: 100)")
    parser.add_argument("--interval", type=int, default=None,
                        help="Seconds between ticks (default: auto from candle interval)")
    parser.add_argument("--candle-interval", type=int, default=5, choices=[1, 5, 15, 30, 60],
                        help="OHLC candle period in minutes (default: 5)")
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

    # Auto-derive tick interval: candle period + 5s buffer for candle close
    auto_interval = candle_interval * 60 + 5
    if args.interval is not None:
        if args.interval < candle_interval * 60:
            print(f"  [WARN] --interval {args.interval}s < candle period {candle_interval}m. Using {auto_interval}s.")
            tick_interval = auto_interval
        else:
            tick_interval = args.interval
    else:
        tick_interval = auto_interval

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
