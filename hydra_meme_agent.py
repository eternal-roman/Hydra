"""APEX Meme Engine — standalone competition-token trading agent.

Isolation guarantee: imports nothing from hydra_engine, hydra_agent,
hydra_brain, hydra_quant_rules, or hydra_pair_registry.
"""
from __future__ import annotations

import argparse
import asyncio
import json
import os
import shlex
import subprocess
import time
import threading
from dataclasses import dataclass, field, asdict
from typing import Optional
import websockets


# ─── Constants ────────────────────────────────────────────────────────────────

WS_PORT = 8766
CANDLE_INTERVAL = 5          # minutes
WARMUP_BARS = 15
CANDLE_BUFFER_SIZE = 20
OBI_POLL_INTERVAL = 10       # seconds
COMPETITION_SCAN_INTERVAL = 900  # 15 minutes
KRAKEN_REST_FLOOR = 2.0      # seconds between CLI calls
RSI_PERIOD = 9
VOL_EMA_PERIOD = 10
OBI_ENTRY_THRESHOLD = 0.20
OBI_BOOK_FADE = -0.20
RSI_ENTRY_LOW = 45
RSI_ENTRY_HIGH = 78
RSI_EXHAUST = 82
VOLUME_SPIKE_MULTIPLIER = 1.8
VOLUME_DEATH_MULTIPLIER = 0.4
ASK_WALL_USD_LIMIT = 500.0
PROFIT_TARGET_PCT = 0.025    # 2.5%
HARD_STOP_PCT = -0.013       # -1.3%
TIME_STOP_CANDLES = 3
OBI_LEVELS = 5
TAKER_SLIPPAGE_BPS = 5       # 0.05% — limit at ask+0.05% for BUY
SLIPPAGE_CAP_BPS = 10        # 0.10% — reject if book moves more

COMPETITION_ANOMALY_RATIO = 5.0
COMPETITION_EMA_ALPHA = 1 / 7

COMPETITION_SEED_PAIRS = [
    "BTC/USD", "ETH/USD", "SOL/USD", "XRP/USD", "ADA/USD",
    "DOT/USD", "LINK/USD", "AVAX/USD", "ATOM/USD", "NEAR/USD",
    "FIL/USD", "APT/USD", "OP/USD", "ARB/USD", "INJ/USD",
    "TIA/USD", "SEI/USD", "PYTH/USD", "WIF/USD", "POPCAT/USD",
    "BONK/USD", "PEPE/USD", "PLAY/USD", "LION/USD",
    "MATIC/USD", "SAND/USD", "MANA/USD", "ENJ/USD", "CHZ/USD",
]


# ─── Data Classes ─────────────────────────────────────────────────────────────

@dataclass
class CandleBar:
    ts: int           # Unix timestamp of bar open
    open: float
    high: float
    low: float
    close: float
    vwap: float
    volume: float
    count: int


@dataclass
class Position:
    entry_price: float
    qty: float
    notional_usd: float
    entry_ts: int
    candles_held: int = 0
    order_id: str = ""


@dataclass
class TradeRecord:
    entry_ts: int
    exit_ts: int
    entry_price: float
    exit_price: float
    qty: float
    gross_pnl: float
    fees_usd: float
    net_pnl: float
    exit_reason: str
    hold_candles: int


# ─── Pure Indicator Functions ──────────────────────────────────────────────────

def wilder_rsi(closes: list[float], period: int = RSI_PERIOD) -> float:
    """Wilder EMA RSI. Returns 50.0 when insufficient data (neutral)."""
    if len(closes) < period + 1:
        return 50.0
    diffs = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [max(d, 0.0) for d in diffs]
    losses = [max(-d, 0.0) for d in diffs]
    avg_gain = sum(gains[:period]) / period
    avg_loss = sum(losses[:period]) / period
    for i in range(period, len(gains)):
        avg_gain = (avg_gain * (period - 1) + gains[i]) / period
        avg_loss = (avg_loss * (period - 1) + losses[i]) / period
    if avg_loss == 0.0:
        return 100.0 if avg_gain > 0 else 50.0
    return 100.0 - (100.0 / (1.0 + avg_gain / avg_loss))


def vol_ema(values: list[float], period: int = VOL_EMA_PERIOD) -> float:
    """Standard EMA with alpha = 2/(period+1). Returns 0.0 for empty input."""
    if not values:
        return 0.0
    alpha = 2.0 / (period + 1)
    result = values[0]
    for v in values[1:]:
        result = alpha * v + (1 - alpha) * result
    return result


def compute_obi(
    bids: list[tuple],
    asks: list[tuple],
    levels: int = OBI_LEVELS,
) -> float:
    """Order Book Imbalance: (bid_depth - ask_depth) / (bid_depth + ask_depth).

    Each entry is (price, qty) as floats or strings. Returns 0.0 on empty book.
    """
    bid_depth = sum(float(p) * float(q) for p, q in bids[:levels])
    ask_depth = sum(float(p) * float(q) for p, q in asks[:levels])
    total = bid_depth + ask_depth
    return (bid_depth - ask_depth) / total if total > 0.0 else 0.0


def compute_vwap(bars: list[CandleBar]) -> float:
    """Close-price VWAP across all provided bars (close * volume weighted).

    Uses close price, not typical price (H+L+C)/3 — intentional for
    compatibility with Kraken OHLC candle format. Returns 0.0 for empty list.
    """
    total_pv = sum(b.close * b.volume for b in bars)
    total_v = sum(b.volume for b in bars)
    return total_pv / total_v if total_v > 0.0 else 0.0


# ─── Signal Engine ─────────────────────────────────────────────────────────────

class SignalEngine:
    """Evaluates 5 entry gates and 6 exit triggers against candle history."""

    def __init__(self):
        self._bars: list[CandleBar] = []
        self._vwap_cum_pv: float = 0.0
        self._vwap_cum_v: float = 0.0

    def add_bar(self, bar: CandleBar) -> None:
        """Add a closed bar to the buffer. Trims to CANDLE_BUFFER_SIZE."""
        self._bars.append(bar)
        self._vwap_cum_pv += bar.close * bar.volume
        self._vwap_cum_v += bar.volume
        if len(self._bars) > CANDLE_BUFFER_SIZE:
            oldest = self._bars.pop(0)
            self._vwap_cum_pv -= oldest.close * oldest.volume
            self._vwap_cum_v -= oldest.volume

    def is_warmed_up(self) -> bool:
        return len(self._bars) >= WARMUP_BARS

    @property
    def session_vwap(self) -> float:
        return self._vwap_cum_pv / self._vwap_cum_v if self._vwap_cum_v > 0 else 0.0

    @property
    def current_rsi(self) -> float:
        closes = [b.close for b in self._bars]
        return wilder_rsi(closes)

    @property
    def vol_ema_baseline(self) -> float:
        volumes = [b.volume for b in self._bars]
        return vol_ema(volumes)

    def evaluate_entry_gates(
        self,
        latest_bar: CandleBar,
        obi: float,
        ask_wall_usd: float,
    ) -> dict:
        """Evaluate all 5 entry gates. Returns dict with gate booleans + all_pass."""
        vol_baseline = self.vol_ema_baseline
        rsi = wilder_rsi([b.close for b in self._bars] + [latest_bar.close])
        vwap = self.session_vwap

        gates = {
            "volume_spike": latest_bar.volume > VOLUME_SPIKE_MULTIPLIER * vol_baseline,
            "obi": obi > OBI_ENTRY_THRESHOLD,
            "vwap_align": latest_bar.close > vwap if vwap > 0 else False,
            "rsi_window": RSI_ENTRY_LOW <= rsi <= RSI_ENTRY_HIGH,
            "ask_wall_clear": ask_wall_usd < ASK_WALL_USD_LIMIT,
            "rsi_value": round(rsi, 1),
            "vwap_value": round(vwap, 8),
            "vol_ema_value": round(vol_baseline, 2),
        }
        gates["all_pass"] = all(gates[k] for k in
                                ["volume_spike", "obi", "vwap_align", "rsi_window", "ask_wall_clear"])
        return gates

    def evaluate_exit_bar(self, position, latest_bar: CandleBar) -> Optional[str]:
        """Bar-close exit triggers: RSI exhaust, time stop, volume death.

        position is a Position dataclass. Returns exit reason string or None.
        """
        rsi = wilder_rsi([b.close for b in self._bars] + [latest_bar.close])
        if rsi > RSI_EXHAUST:
            return "rsi_exhaust"
        if position.candles_held >= TIME_STOP_CANDLES:
            return "time_stop"
        vol_baseline = self.vol_ema_baseline
        if vol_baseline > 0 and latest_bar.volume < VOLUME_DEATH_MULTIPLIER * vol_baseline:
            return "volume_death"
        return None

    def evaluate_exit_intracandle(
        self,
        position,
        mid_price: float,
        obi: float,
    ) -> Optional[str]:
        """10-second exit triggers: profit target, hard stop, book fade.

        position is a Position dataclass. Returns exit reason string or None.
        """
        pct_change = (mid_price - position.entry_price) / position.entry_price
        if pct_change >= PROFIT_TARGET_PCT:
            return "profit_target"
        if pct_change <= HARD_STOP_PCT:
            return "hard_stop"
        if obi < OBI_BOOK_FADE:
            return "book_fade"
        return None


# ─── Competition Detector ──────────────────────────────────────────────────────

class CompetitionDetector:
    """Monitors token volume baselines and detects competition anomalies."""

    def __init__(self, watchlist_path: str):
        self._path = watchlist_path
        self._lock = threading.Lock()
        self._data: dict = self._load_or_bootstrap()

    def _load_or_bootstrap(self) -> dict:
        if os.path.exists(self._path):
            with open(self._path) as f:
                return json.load(f)
        data = {
            "tokens": [
                {
                    "pair": p,
                    "baseline_volume_7d": None,
                    "last_updated": None,
                    "competition_type": None,
                    "competition_type_confirmed": False,
                    "alert_suppressed_until": None,
                }
                for p in COMPETITION_SEED_PAIRS
            ],
            "last_scan": None,
        }
        self._save(data)
        return data

    def _save(self, data: dict) -> None:
        tmp = self._path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(data, f, indent=2)
        os.replace(tmp, self._path)

    def _find_token(self, pair: str) -> Optional[dict]:
        for t in self._data["tokens"]:
            if t["pair"] == pair:
                return t
        return None

    def _find_or_add_token(self, pair: str) -> dict:
        token = self._find_token(pair)
        if token is None:
            token = {
                "pair": pair,
                "baseline_volume_7d": None,
                "last_updated": None,
                "competition_type": None,
                "competition_type_confirmed": False,
                "alert_suppressed_until": None,
            }
            self._data["tokens"].append(token)
        return token

    def _set_baseline(self, pair: str, volume: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            token["baseline_volume_7d"] = volume
            token["last_updated"] = int(time.time())
            self._save(self._data)

    def _get_baseline(self, pair: str) -> Optional[float]:
        token = self._find_token(pair)
        return token["baseline_volume_7d"] if token else None

    def _update_baseline(self, pair: str, volume: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            old = token["baseline_volume_7d"]
            if old is None:
                token["baseline_volume_7d"] = volume
            else:
                token["baseline_volume_7d"] = (
                    COMPETITION_EMA_ALPHA * volume + (1 - COMPETITION_EMA_ALPHA) * old
                )
            token["last_updated"] = int(time.time())
            self._save(self._data)

    def _is_anomaly(self, pair: str, current_volume: float) -> bool:
        baseline = self._get_baseline(pair)
        if baseline is None or baseline <= 0:
            return False
        return (current_volume / baseline) >= COMPETITION_ANOMALY_RATIO

    def _suppress(self, pair: str, until: float) -> None:
        with self._lock:
            token = self._find_or_add_token(pair)
            token["alert_suppressed_until"] = until
            self._save(self._data)

    def _is_suppressed(self, pair: str) -> bool:
        token = self._find_token(pair)
        if token is None:
            return False
        until = token.get("alert_suppressed_until")
        return until is not None and time.time() < until

    def infer_competition_type(self, pair: str) -> str:
        """Volume-pattern heuristic. Returns 'volume', 'pnl', 'rebate', or 'unknown'."""
        token = self._find_token(pair)
        if token and token.get("competition_type_confirmed"):
            return token["competition_type"]
        baseline = self._get_baseline(pair)
        if baseline is None:
            return "unknown"
        return "volume"

    def get_all_tokens(self) -> list[dict]:
        return list(self._data.get("tokens", []))


# ─── Session State ─────────────────────────────────────────────────────────────

@dataclass
class SessionState:
    pair: str = ""
    engine_state: str = "idle"   # idle | warmup | running | halted
    candle_buffer: list = field(default_factory=list)
    open_position: Optional[dict] = None
    session_pnl: float = 0.0
    daily_pnl: float = 0.0
    trade_count: int = 0


def save_session(state: SessionState, path: str) -> None:
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(asdict(state), f, indent=2)
    os.replace(tmp, path)


def load_session(path: str) -> SessionState:
    with open(path) as f:
        data = json.load(f)
    return SessionState(**{k: v for k, v in data.items() if k in SessionState.__dataclass_fields__})


def append_journal(record: TradeRecord, path: str) -> None:
    existing: list = []
    if os.path.exists(path):
        with open(path) as f:
            existing = json.load(f)
    existing.append(asdict(record))
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(existing, f, indent=2)
    os.replace(tmp, path)


# ─── Kraken CLI ────────────────────────────────────────────────────────────────

def _kraken_cli(args: list[str], timeout: int = 20) -> dict:
    """Execute a kraken CLI command via WSL and return parsed JSON.

    All args are shlex-quoted to prevent injection (matches hydra_kraken_cli.py pattern).
    """
    quoted = " ".join(shlex.quote(str(a)) for a in args)
    cmd_str = "source ~/.cargo/env"
    api_key = os.environ.get("KRAKEN_API_KEY")
    api_secret = os.environ.get("KRAKEN_API_SECRET")
    if api_key and api_secret:
        cmd_str += (f" && export KRAKEN_API_KEY={shlex.quote(api_key)}"
                    f" && export KRAKEN_API_SECRET={shlex.quote(api_secret)}")
    cmd_str += f" && kraken {quoted} -o json 2>/dev/null"
    cmd = ["wsl", "-d", "Ubuntu", "--", "bash", "-c", cmd_str]
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        stdout = result.stdout.strip()
        if not stdout:
            return {"error": f"Empty response (exit {result.returncode})"}
        data = json.loads(stdout)
        if isinstance(data, dict) and "error" in data:
            return data
        return data
    except subprocess.TimeoutExpired:
        return {"error": "timeout", "retryable": True}
    except json.JSONDecodeError as e:
        return {"error": f"JSON parse: {e}"}
    except Exception as e:
        return {"error": str(e)}
