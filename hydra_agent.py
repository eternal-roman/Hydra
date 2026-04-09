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
import signal as sig
import asyncio
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

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

    # ─── Order Execution ───

    @staticmethod
    def order_buy(pair: str, volume: float, price: float = None,
                  order_type: str = "limit", post_only: bool = True,
                  validate: bool = False) -> dict:
        """Place a buy order. Defaults to limit post-only (maker)."""
        p = KrakenCLI._resolve_pair(pair)
        args = ["order", "buy", p, f"{volume:.8f}", "--type", order_type, "--yes"]
        if price is not None and order_type != "market":
            args.extend(["--price", f"{price:.8f}"])
        if post_only and order_type == "limit":
            args.extend(["--oflags", "post"])
        if validate:
            args.append("--validate")
        return KrakenCLI._run(args)

    @staticmethod
    def order_sell(pair: str, volume: float, price: float = None,
                   order_type: str = "limit", post_only: bool = True,
                   validate: bool = False) -> dict:
        """Place a sell order. Defaults to limit post-only (maker)."""
        p = KrakenCLI._resolve_pair(pair)
        args = ["order", "sell", p, f"{volume:.8f}", "--type", order_type, "--yes"]
        if price is not None and order_type != "market":
            args.extend(["--price", f"{price:.8f}"])
        if post_only and order_type == "limit":
            args.extend(["--oflags", "post"])
        if validate:
            args.append("--validate")
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
# ORDER RECONCILER
# ═══════════════════════════════════════════════════════════════

class OrderReconciler:
    """Polls Kraken open-orders and detects orders that disappeared from the
    exchange (filled, cancelled by dead-man's-switch, rejected).  Prevents
    silent divergence between the agent's local order registry and reality."""

    def __init__(self, poll_every_ticks: int = 5):
        self.poll_every_ticks = poll_every_ticks
        self.known_orders: Dict[str, dict] = {}  # txid → {pair, side, amount, registered_at}

    def register(self, txid: str, pair: str, side: str, amount: float):
        """Track a newly placed order by its Kraken txid."""
        if txid and txid != "unknown":
            self.known_orders[txid] = {
                "pair": pair, "side": side, "amount": amount,
                "registered_at": time.time(),
            }

    def maybe_reconcile(self, tick: int) -> List[dict]:
        """Poll open-orders every N ticks. Returns events for disappeared orders."""
        if tick % self.poll_every_ticks != 0 or not self.known_orders:
            return []
        try:
            result = KrakenCLI.open_orders()
            if isinstance(result, dict) and "error" in result:
                return [{"type": "poll_failed", "error": result["error"]}]
            # Extract live txids from Kraken response
            live_txids: set = set()
            if isinstance(result, dict):
                opens = result.get("open", result.get("result", result))
                if isinstance(opens, dict):
                    live_txids = set(opens.keys())
            # Detect disappeared orders
            events: List[dict] = []
            for txid in list(self.known_orders.keys()):
                if txid not in live_txids:
                    info = self.known_orders.pop(txid)
                    events.append({
                        "type": "order_disappeared",
                        "txid": txid,
                        "pair": info["pair"],
                        "side": info["side"],
                        "amount": info["amount"],
                    })
            return events
        except Exception as e:
            return [{"type": "poll_failed", "error": str(e)}]


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
    TRADE_LOG_CAP = 2000            # Bound in-memory trade log
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
        self.trade_log = []
        self._snapshot_dir = os.path.dirname(os.path.abspath(__file__))
        self._kraken_lock = threading.Lock()  # Serialize Kraken API calls across threads
        self._completed_trades_since_update = 0  # Counter for tuner update cadence

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

        # Order reconciler — detects filled/cancelled orders (live mode only)
        self.reconciler = OrderReconciler(poll_every_ticks=5) if not paper else None

        # Track previous regime for cross-pair swap triggers
        self.prev_regimes: Dict[str, str] = {}

        # Restore from snapshot if requested
        if resume:
            self._load_snapshot()

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
        # Flush session snapshot for --resume
        try:
            self._save_snapshot()
        except Exception as e:
            print(f"  [HYDRA] Snapshot flush failed: {e}")

    # ─── Session snapshot (atomic JSON; resumable across runs) ─────────────

    def _snapshot_path(self) -> str:
        return os.path.join(self._snapshot_dir, "hydra_session_snapshot.json")

    def _save_snapshot(self):
        """Atomically save session state to disk (.tmp → os.replace)."""
        snapshot = {
            "version": 1,
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "mode": self.mode,
            "paper": self.paper,
            "pairs": self.pairs,
            "competition_start_balance": self._competition_start_balance,
            "engines": {pair: eng.snapshot_runtime() for pair, eng in self.engines.items()},
            "coordinator_regime_history": self.coordinator.regime_history,
            "trade_log": self.trade_log[-200:],
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
            self.trade_log = list(snapshot.get("trade_log", []))
            if snapshot.get("competition_start_balance") is not None:
                self._competition_start_balance = float(snapshot["competition_start_balance"])
            print(f"  [SNAPSHOT] Restored session from {snapshot.get('timestamp', '?')}")
        except Exception as e:
            print(f"  [SNAPSHOT] Load failed: {e}, starting fresh.")

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

        print(f"\n  [HYDRA] Starting LIVE trading loop")
        print(f"  [HYDRA] Pairs: {', '.join(self.pairs)}")
        print(f"  [HYDRA] Interval: {self.interval}s | Duration: {self.duration}s")
        print(f"  {'='*80}")

        tick = 0
        while self.running and (self.duration == 0 or (time.time() - self.start_time) < self.duration):
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

            # Print status and execute trades (sequential — rate limiting required)
            # Skip swap pairs — the swap handler manages their execution.
            for pair in self.pairs:
                state = all_states.get(pair)
                if state:
                    self._print_tick_status(pair, state)
                    if state.get("last_trade") and pair not in swap_pairs:
                        success = self._execute_trade(pair, state["last_trade"])
                        if not success and state.get("_pre_trade_snapshot"):
                            engine = self.engines[pair]
                            engine.restore_position(state["_pre_trade_snapshot"])
                            print(f"  [ROLLBACK] {pair}: engine state rolled back after failed order")

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

            # Reconcile tracked orders against exchange state (live mode only)
            if self.reconciler and self.reconciler.known_orders and tick % self.reconciler.poll_every_ticks == 0:
                time.sleep(2)  # Rate limit before Kraken API call
            if self.reconciler:
                for ev in self.reconciler.maybe_reconcile(tick):
                    if ev["type"] == "order_disappeared":
                        print(f"  [RECON] {ev['pair']} {ev['side'].upper()} {ev['txid']} "
                              f"no longer on exchange (filled/cancelled)")
                    elif ev["type"] == "poll_failed":
                        print(f"  [RECON] Poll failed: {ev.get('error')}")

            # Rolling save — persist trade log every tick so no data is lost on crash
            if self.trade_log:
                rolling_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hydra_trades_live.json")
                try:
                    with open(rolling_file, "w") as f:
                        json.dump(self.trade_log, f, indent=2)
                except Exception:
                    pass

            # Cap trade log to prevent unbounded memory growth
            if len(self.trade_log) > self.TRADE_LOG_CAP:
                self.trade_log = self.trade_log[-self.TRADE_LOG_CAP:]

            # Periodic session snapshot for --resume
            if tick % self.SNAPSHOT_EVERY_N_TICKS == 0:
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

    def _execute_trade(self, pair: str, trade: dict) -> bool:
        """Execute a trade via kraken-cli — paper or live limit post-only.

        Returns True if the order was accepted by the exchange, False otherwise.
        The caller should rollback engine state on False to prevent phantom positions.
        """
        action = trade["action"].lower()
        amount = trade["amount"]

        if self.paper:
            return self._execute_paper_trade(pair, action, amount, trade)

        # ─── Live: limit post-only ───
        time.sleep(2)
        ticker = KrakenCLI.ticker(pair)
        if "error" in ticker or "bid" not in ticker:
            print(f"  [TRADE] Cannot fetch ticker for {pair}, skipping")
            self.trade_log.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "pair": pair, "action": trade["action"], "amount": amount,
                "price": trade["price"], "status": "TICKER_FAILED",
                "error": ticker.get("error", "no bid/ask"),
            })
            return False

        limit_price = ticker["bid"] if action == "buy" else ticker["ask"]

        # Validate
        time.sleep(2)
        print(f"  [TRADE] Validating {action.upper()} {amount:.8f} {pair} @ {limit_price} (post-only limit)...")
        if action == "buy":
            val_result = KrakenCLI.order_buy(pair, amount, price=limit_price, validate=True)
        else:
            val_result = KrakenCLI.order_sell(pair, amount, price=limit_price, validate=True)

        if "error" in val_result:
            print(f"  [TRADE] Validation failed: {val_result['error']}")
            self.trade_log.append({
                "time": datetime.now(timezone.utc).isoformat(),
                "pair": pair, "action": trade["action"], "amount": amount,
                "price": limit_price, "status": "VALIDATION_FAILED",
                "error": val_result["error"],
            })
            return False

        # Re-fetch ticker for fresh bid/ask — price may have moved during validation
        time.sleep(2)
        fresh_ticker = KrakenCLI.ticker(pair)
        if "error" not in fresh_ticker and "bid" in fresh_ticker:
            limit_price = fresh_ticker["bid"] if action == "buy" else fresh_ticker["ask"]

        print(f"  [TRADE] Executing {action.upper()} {amount:.8f} {pair} @ {limit_price} (limit post-only)...")
        if action == "buy":
            result = KrakenCLI.order_buy(pair, amount, price=limit_price)
        else:
            result = KrakenCLI.order_sell(pair, amount, price=limit_price)

        if "error" in result:
            print(f"  [TRADE] FAILED: {result['error']}")
            status = "FAILED"
            success = False
        else:
            txid = result.get("txid", result.get("result", {}).get("txid", "unknown"))
            # Kraken API may return txid as a list — unwrap to string
            if isinstance(txid, list):
                txid = txid[0] if txid else "unknown"
            print(f"  [TRADE] SUCCESS: {action.upper()} {amount:.8f} {pair} | txid: {txid}")
            status = "EXECUTED"
            success = True
            if self.reconciler:
                self.reconciler.register(txid, pair, action, amount)

        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "pair": pair,
            "action": trade["action"],
            "amount": amount,
            "price": limit_price,
            "order_type": "limit post-only",
            "reason": trade["reason"],
            "confidence": trade.get("confidence"),
            "status": status,
            "result": result if "error" not in result else None,
            "error": result.get("error"),
        })
        return success

    def _execute_paper_trade(self, pair: str, action: str, amount: float, trade: dict) -> bool:
        """Execute a paper trade via kraken-cli paper commands."""
        time.sleep(2)
        print(f"  [PAPER] Executing {action.upper()} {amount:.8f} {pair} (paper market)...")
        if action == "buy":
            result = KrakenCLI.paper_buy(pair, amount)
        else:
            result = KrakenCLI.paper_sell(pair, amount)

        if "error" in result:
            print(f"  [PAPER] FAILED: {result['error']}")
            status = "PAPER_FAILED"
            success = False
        else:
            print(f"  [PAPER] SUCCESS: {action.upper()} {amount:.8f} {pair}")
            status = "PAPER_EXECUTED"
            success = True

        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "pair": pair,
            "action": trade["action"],
            "amount": amount,
            "price": trade["price"],
            "order_type": "paper market",
            "reason": trade["reason"],
            "confidence": trade.get("confidence"),
            "status": status,
            "result": result if "error" not in result else None,
            "error": result.get("error"),
        })
        return success

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
        }
        if not self._execute_trade(sell_pair, sell_trade):
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
        }
        if not self._execute_trade(buy_pair, buy_trade):
            buy_engine.restore_position(buy_snap)
            print(f"  [ROLLBACK] {buy_pair}: engine state rolled back after failed swap buy")
            return

        # Log the coordinated swap
        self.trade_log.append({
            "time": datetime.now(timezone.utc).isoformat(),
            "type": "COORDINATED_SWAP",
            "swap_id": swap_id,
            "sell_pair": sell_pair,
            "buy_pair": buy_pair,
            "sell_amount": sell_trade_obj.amount,
            "buy_amount": buy_trade_obj.amount,
            "reason": reason,
        })
        print(f"  [SWAP] Swap {swap_id} complete")

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
            "pairs": pairs_data,
            "trade_log": self.trade_log[-20:],
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

        # Trade log
        if self.trade_log:
            print(f"\n  TRADE LOG ({len(self.trade_log)} entries)")
            print(f"  {'-'*70}")
            for t in self.trade_log[-20:]:
                if t.get("type") == "COORDINATED_SWAP":
                    print(f"  [SW] {t['time']} | SWAP {t.get('sell_pair','?')} → {t.get('buy_pair','?')} | {t.get('reason','')[:40]}")
                    continue
                status_icon = "OK" if t.get("status") == "EXECUTED" else "XX"
                t_pair = t.get('pair', '?')
                t_cur = "$" if t_pair.endswith("USDC") or t_pair.endswith("USD") else ""
                print(f"  [{status_icon}] {t['time']} | {t.get('action','?'):<4} {t.get('amount', 0):.8f} {t_pair:<10} "
                      f"@ {t_cur}{t.get('price', 0):>10,.{4 if t_cur else 8}f} | {t.get('status','?')}")
        else:
            print(f"\n  No trades executed during session.")

        # Export trade log
        ts = int(time.time())
        base_dir = os.path.dirname(os.path.abspath(__file__))
        log_file = os.path.join(base_dir, f"hydra_trades_{ts}.json")
        try:
            with open(log_file, "w") as f:
                json.dump(self.trade_log, f, indent=2)
            print(f"\n  Trade log exported to: {log_file}")
        except Exception as e:
            print(f"\n  [WARN] Could not export trade log: {e}")

        # Export competition results summary
        self._export_competition_results(base_dir, ts)

        print(f"\n  Past performance does not guarantee future results. Not financial advice.")
        print(f"  {'='*80}")

    def _compute_pair_realized_pnl(self, pair: str) -> float:
        """Compute realized P&L for a pair from trade history.

        Sums sell revenue minus buy cost across all trades in the log.
        This is accurate across resumes because it uses actual trade prices,
        not engine balances which get pooled and re-split on each restart.
        """
        buy_cost = 0.0
        sell_revenue = 0.0
        for t in self.trade_log:
            if t.get("pair") != pair or t.get("type") == "COORDINATED_SWAP":
                continue
            # Count all trades the engine committed (EXECUTED and FAILED-but-committed).
            # With rollback logic, truly failed trades are rolled back and not in the
            # engine state. Pre-rollback FAILED trades that were committed remain in
            # the log and are correct to include (e.g., the SOL/USDC BUY that Kraken
            # accepted despite returning an API error).
            amt = t.get("amount", 0)
            price = t.get("price", 0)
            if t["action"] == "BUY":
                buy_cost += amt * price
            elif t["action"] == "SELL":
                sell_revenue += amt * price
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
            "trade_log": self.trade_log,
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
