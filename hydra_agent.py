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
from datetime import datetime, timezone
from typing import Dict, List, Optional, Any

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from hydra_engine import HydraEngine, SIZING_CONSERVATIVE, SIZING_COMPETITION

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

    def __init__(
        self,
        pairs: List[str],
        initial_balance: float = 100.0,
        interval_seconds: int = 60,
        duration_seconds: int = 600,
        ws_port: int = 8765,
        mode: str = "conservative",
        paper: bool = False,
    ):
        self.pairs = pairs
        self.initial_balance = initial_balance
        self.interval = interval_seconds
        self.duration = duration_seconds
        self.mode = mode
        self.paper = paper
        self.running = True
        self.start_time = None
        self.trade_log = []

        # Sizing config based on mode
        sizing = SIZING_COMPETITION if mode == "competition" else SIZING_CONSERVATIVE

        # One engine per pair
        self.engines: Dict[str, HydraEngine] = {}
        for pair in pairs:
            self.engines[pair] = HydraEngine(
                initial_balance=initial_balance / len(pairs),
                asset=pair,
                sizing=sizing,
            )

        # Dashboard broadcaster
        self.broadcaster = DashboardBroadcaster(port=ws_port)

        # Track previous regime for cross-pair swap triggers
        self.prev_regimes: Dict[str, str] = {}

        # Graceful shutdown
        sig.signal(sig.SIGINT, self._handle_shutdown)
        sig.signal(sig.SIGTERM, self._handle_shutdown)

    def _handle_shutdown(self, signum, frame):
        print("\n\n  [HYDRA] Shutdown signal received. Generating final report...\n")
        self.running = False

    def run(self):
        """Main agent loop."""
        self.start_time = time.time()
        self._print_banner()

        # Start WebSocket server for dashboard
        self.broadcaster.start()
        time.sleep(0.5)

        # Set dead man's switch (live mode only)
        if not self.paper:
            print("  [HYDRA] Setting dead man's switch (60s)...")
            result = KrakenCLI.cancel_after(60)
            if "error" not in result:
                print("  [HYDRA] Dead man's switch active")
            else:
                print(f"  [WARN] Dead man's switch: {result.get('error', 'unknown')}")

        # Check account balance
        print("  [HYDRA] Checking account balance...")
        bal = KrakenCLI.balance()
        if "error" not in bal:
            for asset, amount in bal.items():
                print(f"  [HYDRA]   {asset}: {amount}")
        else:
            print(f"  [WARN] Balance check failed: {bal}")

        # Warmup: fetch historical candles for each pair
        print("\n  [HYDRA] Warming up with historical candles...")
        for pair in self.pairs:
            candles = KrakenCLI.ohlc(pair, interval=1)
            if candles:
                for c in candles[-200:]:
                    self.engines[pair].ingest_candle(c)
                price = candles[-1]["close"]
                print(f"  [HYDRA] {pair}: {min(len(candles), 200)} candles loaded, last price: ${price:,.4f}")
            else:
                print(f"  [WARN] {pair}: no historical data")
            time.sleep(2)  # Respect rate limits

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
                KrakenCLI.cancel_after(60)
                time.sleep(2)  # Rate limit

            all_states = {}
            for pair in self.pairs:
                state = self._process_pair(pair)
                time.sleep(2)  # Rate limit after OHLC fetch
                if state:
                    all_states[pair] = state
                    self._print_tick_status(pair, state)

                    # Execute trade if signal is actionable
                    if state.get("last_trade"):
                        self._execute_trade(pair, state["last_trade"])
                        # _execute_trade has its own 2s rate limits internally

            # Check for regime-driven cross-pair swaps
            self._check_cross_pair_swaps(all_states)

            # Broadcast state to dashboard (uses cached balance, no extra API call)
            dashboard_state = self._build_dashboard_state(tick, all_states, elapsed, remaining)
            self.broadcaster.broadcast(dashboard_state)

            # Rolling save — persist trade log every tick so no data is lost on crash
            if self.trade_log:
                rolling_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "hydra_trades_live.json")
                try:
                    with open(rolling_file, "w") as f:
                        json.dump(self.trade_log, f, indent=2)
                except Exception:
                    pass

            # Sleep until next tick
            next_tick_time = self.start_time + tick * self.interval
            sleep_time = next_tick_time - time.time()
            if sleep_time > 0 and self.running:
                time.sleep(sleep_time)

        # Final report
        self._print_final_report()

    def _process_pair(self, pair: str) -> Optional[dict]:
        """Fetch latest data and run engine tick for one pair."""
        engine = self.engines[pair]

        # Fetch latest candle
        candles = KrakenCLI.ohlc(pair, interval=1)
        if candles:
            engine.ingest_candle(candles[-1])
        else:
            # Fallback to ticker
            ticker = KrakenCLI.ticker(pair)
            if "price" in ticker:
                p = ticker["price"]
                engine.ingest_candle({
                    "open": p, "high": p, "low": p, "close": p, "volume": 0,
                })

        return engine.tick()

    def _execute_trade(self, pair: str, trade: dict):
        """Execute a trade via kraken-cli — paper or live limit post-only."""
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
            return

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
            return

        # Execute
        time.sleep(2)
        print(f"  [TRADE] Executing {action.upper()} {amount:.8f} {pair} @ {limit_price} (limit post-only)...")
        if action == "buy":
            result = KrakenCLI.order_buy(pair, amount, price=limit_price)
        else:
            result = KrakenCLI.order_sell(pair, amount, price=limit_price)

        if "error" in result:
            print(f"  [TRADE] FAILED: {result['error']}")
            status = "FAILED"
        else:
            txid = result.get("txid", result.get("result", {}).get("txid", "unknown"))
            print(f"  [TRADE] SUCCESS: {action.upper()} {amount:.8f} {pair} | txid: {txid}")
            status = "EXECUTED"

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

    def _execute_paper_trade(self, pair: str, action: str, amount: float, trade: dict):
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
        else:
            print(f"  [PAPER] SUCCESS: {action.upper()} {amount:.8f} {pair}")
            status = "PAPER_EXECUTED"

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

    def _check_cross_pair_swaps(self, all_states: Dict[str, dict]):
        """
        Check if a regime change warrants a cross-pair swap.
        When SOL/USDC regime shifts to TREND_DOWN and SOL/XBT regime is
        TREND_UP or RANGING, consider swapping SOL for BTC via SOL/XBT.
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

    def _build_dashboard_state(self, tick: int, all_states: dict,
                                elapsed: float, remaining: float) -> dict:
        """Build the full state dict for the dashboard WebSocket."""
        # Fetch balance every 5th tick to reduce API calls
        if tick % 5 == 1 or not hasattr(self, '_cached_balance'):
            bal = KrakenCLI.balance()
            self._cached_balance = bal if "error" not in bal else getattr(self, '_cached_balance', {})
            time.sleep(2)  # Rate limit
        bal = getattr(self, '_cached_balance', {})

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
            "pairs": pairs_data,
            "trade_log": self.trade_log[-20:],
            "running": self.running,
        }

    def _print_tick_status(self, pair: str, state: dict):
        """Print concise tick status."""
        s = state["signal"]
        p = state["portfolio"]
        pos = state["position"]

        signal_icon = {"BUY": "^", "SELL": "v", "HOLD": "-"}.get(s["action"], "?")

        print(f"  | {pair:<10} | ${state['price']:>10,.4f} | "
              f"{state['regime']:<10} -> {state['strategy']:<15} | "
              f"{signal_icon} {s['action']:<4} ({s['confidence']:.2f}) | "
              f"Eq: ${p['equity']:>10,.2f} | "
              f"P&L: {p['pnl_pct']:>+.2f}% | DD: {p['max_drawdown_pct']:.1f}%")

        if pos["size"] > 0:
            print(f"  |            | Pos: {pos['size']:.8f} @ ${pos['avg_entry']:,.4f} | "
                  f"Unrealized: ${pos['unrealized_pnl']:>+,.2f}")

        if state.get("last_trade"):
            t = state["last_trade"]
            profit_str = f" | Profit: ${t['profit']:+,.2f}" if t.get("profit") is not None else ""
            print(f"  |  >>> SIGNAL: {t['action']} {t['amount']:.8f} @ ${t['price']:,.4f}{profit_str}")
            print(f"  |      Reason: {t['reason'][:75]}")

    def _print_banner(self):
        trade_mode = "PAPER" if self.paper else "LIVE"
        sizing_mode = self.mode.upper()
        print("")
        print("  HYDRA - Hyper-adaptive Dynamic Regime-switching Universal Agent")
        print("  ================================================================")
        print(f"  Trading: {trade_mode} | Sizing: {sizing_mode} | Kraken CLI v0.2.3 (WSL)")
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
                status_icon = "OK" if t["status"] == "EXECUTED" else "XX"
                print(f"  [{status_icon}] {t['time']} | {t['action']:<4} {t['amount']:.8f} {t['pair']:<10} "
                      f"@ ${t['price']:>10,.4f} | {t['status']}")
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

    def _export_competition_results(self, base_dir: str, ts: int):
        """Export a competition_results.json for submission proof."""
        elapsed = time.time() - self.start_time if self.start_time else 0
        pair_results = {}
        total_pnl = 0.0
        total_trades = 0

        for pair in self.pairs:
            engine = self.engines[pair]
            price = engine.prices[-1] if engine.prices else 0
            equity = engine.balance + engine.position.size * price
            pnl = equity - engine.initial_balance
            total_pnl += pnl
            total_trades += engine.total_trades
            win_rate = (engine.win_count / (engine.win_count + engine.loss_count) * 100) if (engine.win_count + engine.loss_count) > 0 else 0

            pair_results[pair] = {
                "initial_balance": engine.initial_balance,
                "final_equity": round(equity, 4),
                "net_pnl": round(pnl, 4),
                "pnl_pct": round((pnl / engine.initial_balance) * 100, 4) if engine.initial_balance > 0 else 0,
                "max_drawdown_pct": round(engine.max_drawdown, 4),
                "total_trades": engine.total_trades,
                "wins": engine.win_count,
                "losses": engine.loss_count,
                "win_rate_pct": round(win_rate, 2),
                "sharpe": round(engine._calc_sharpe(), 4),
                "final_price": round(price, 8),
                "position_size": round(engine.position.size, 8),
            }

        results = {
            "agent": "HYDRA",
            "version": "1.1.0",
            "mode": self.mode,
            "paper": self.paper,
            "timestamp_start": datetime.fromtimestamp(self.start_time, tz=timezone.utc).isoformat() if self.start_time else None,
            "timestamp_end": datetime.now(timezone.utc).isoformat(),
            "duration_seconds": round(elapsed, 1),
            "pairs": self.pairs,
            "total_initial_balance": self.initial_balance,
            "total_net_pnl": round(total_pnl, 4),
            "total_pnl_pct": round((total_pnl / self.initial_balance) * 100, 4) if self.initial_balance > 0 else 0,
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
    parser.add_argument("--interval", type=int, default=30,
                        help="Seconds between ticks (default: 30)")
    parser.add_argument("--duration", type=int, default=0,
                        help="Total duration in seconds (default: 0 = run forever, Ctrl+C to stop)")
    parser.add_argument("--ws-port", type=int, default=8765,
                        help="WebSocket port for dashboard (default: 8765)")
    parser.add_argument("--mode", type=str, default="conservative",
                        choices=["conservative", "competition"],
                        help="Sizing mode: conservative (quarter-Kelly) or competition (half-Kelly)")
    parser.add_argument("--paper", action="store_true",
                        help="Use paper trading (no API keys needed, no real money)")

    args = parser.parse_args()
    pairs = [p.strip() for p in args.pairs.split(",")]

    if args.paper:
        print(f"\n  HYDRA — Paper trading mode. No real money at risk.")
    else:
        print(f"\n  WARNING: HYDRA will execute REAL trades on Kraken.")
    print(f"  Pairs: {', '.join(pairs)}")
    print(f"  Mode: {args.mode} | Balance ref: ${args.balance}")
    print(f"  Duration: {args.duration}s")
    if not args.paper:
        print(f"  Dead man's switch will be active.")
    print()

    agent = HydraAgent(
        pairs=pairs,
        initial_balance=args.balance,
        interval_seconds=args.interval,
        duration_seconds=args.duration,
        ws_port=args.ws_port,
        mode=args.mode,
        paper=args.paper,
    )
    agent.run()


if __name__ == "__main__":
    main()
