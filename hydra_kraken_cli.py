"""Hydra Kraken CLI Wrapper."""
import subprocess
import json
import time
import os
import shlex
import asyncio
import threading
import secrets
from pathlib import Path
from typing import Dict, List, Optional, Any, Tuple
from datetime import datetime, timezone

# ═══════════════════════════════════════════════════════════════
# KRAKEN CLI WRAPPER (via WSL)
# ═══════════════════════════════════════════════════════════════

class KrakenCLI:
    """Wraps kraken-cli v0.3.2 running in WSL Ubuntu.

    Verified compatible with kraken-cli v0.3.2 (commit aa32814+):
      - `--asset-class` flag is canonical (`--aclass` is hidden alias);
        Hydra never passed `--aclass`, so no callsite change required.
      - `relativeFundingRate` rename in commit 910a4d6 was internal to
        kraken-cli's paper-trading futures engine. Hydra calls
        `kraken futures tickers` (read-only public endpoint), which still
        emits `fundingRate` (absolute, USD/contract/period) — that field
        is converted to relative bps via `_absolute_to_relative_bps` in
        `hydra_derivatives_stream.py`.
      - Spot endpoints (ticker/balance/orderbook/ohlc/orders/pairs) have
        no breaking schema changes from v0.2.3 → v0.3.2.
    """

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

    # Suffixes Kraken uses for non-tradable (staked/bonded/locked/earn) assets.
    # .F = earn-flex (yield-bearing, instant-redeem) — e.g. USDC.F. v2.16.2
    # adds .F to the suffix set: previously USDC.F normalized to itself,
    # missed the "USDC"→1.0 USD price lookup, and was silently valued at $0
    # in the dashboard's balance history chart + Total Balance stat.
    STAKED_SUFFIXES = ('.B', '.S', '.M', '.F')

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
        """Execute a kraken CLI command via WSL and return parsed JSON.

        Every arg is passed through `shlex.quote` before being joined
        into the bash -c string — internal callers use typed numerics
        and known-good pair names today, but the companion/dashboard
        surface is growing and a single unescaped caller would grant
        RCE in the WSL environment. v2.15.0 hardens the boundary.
        """
        quoted = " ".join(shlex.quote(str(a)) for a in args)
        
        # Multi-tenancy: inject dynamic API keys if provided in the process environment
        cmd_str = "source ~/.cargo/env"
        api_key = os.environ.get("KRAKEN_API_KEY")
        api_secret = os.environ.get("KRAKEN_API_SECRET")
        if api_key and api_secret:
            cmd_str += f" && export KRAKEN_API_KEY={shlex.quote(api_key)} && export KRAKEN_API_SECRET={shlex.quote(api_secret)}"
            
        cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
        cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd_str]
        try:
            result = subprocess.run(
                cmd, capture_output=True, text=True, timeout=timeout,
            )
            stdout = result.stdout.strip()
            rc = result.returncode
            if not stdout:
                return {"error": f"Empty response (exit code {rc})"}
            data = json.loads(stdout)
            if isinstance(data, dict) and "error" in data:
                return data
            if rc != 0:
                # Non-zero exit with parseable stdout: surface the failure so
                # callers don't treat partial output as success.
                return {"error": f"Non-zero exit code {rc}", "partial": data}
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
    def paper_buy(pair: str, volume: float, order_type: str = "limit") -> dict:
        """Paper trade buy — no API keys needed."""
        p = KrakenCLI._resolve_pair(pair)
        return KrakenCLI._run(["paper", "buy", p, "--type", order_type, "--volume", f"{volume:.8f}"])

    @staticmethod
    def paper_sell(pair: str, volume: float, order_type: str = "limit") -> dict:
        """Paper trade sell — no API keys needed."""
        p = KrakenCLI._resolve_pair(pair)
        return KrakenCLI._run(["paper", "sell", p, "--type", order_type, "--volume", f"{volume:.8f}"])

    @staticmethod
    def paper_balance() -> dict:
        """Get paper trading balance."""
        return KrakenCLI._run(["paper", "balance"])



