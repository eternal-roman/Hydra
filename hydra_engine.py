#!/usr/bin/env python3
"""
HYDRA Engine — Hyper-adaptive Dynamic Regime-switching Universal Agent
Core strategy engine: indicators, regime detection, signal generation, position sizing.
Portable pure-Python. No dependencies beyond standard library + json.

Usage:
    from hydra_engine import HydraEngine
    engine = HydraEngine()
    engine.ingest_candle({"open": 95000, "high": 95500, "low": 94500, "close": 95200, "volume": 150})
    state = engine.tick()
    print(state)  # {'regime': 'RANGING', 'strategy': 'MEAN_REVERSION', 'signal': {...}, ...}
"""

import math
import time
import json
from enum import Enum
from dataclasses import dataclass, field, asdict
from typing import List, Optional, Dict, Any


# ═══════════════════════════════════════════════════════════════
# ENUMS
# ═══════════════════════════════════════════════════════════════

class Regime(str, Enum):
    TREND_UP = "TREND_UP"
    TREND_DOWN = "TREND_DOWN"
    RANGING = "RANGING"
    VOLATILE = "VOLATILE"

class Strategy(str, Enum):
    MOMENTUM = "MOMENTUM"
    MEAN_REVERSION = "MEAN_REVERSION"
    GRID = "GRID"
    DEFENSIVE = "DEFENSIVE"

class SignalAction(str, Enum):
    BUY = "BUY"
    SELL = "SELL"
    HOLD = "HOLD"


# ═══════════════════════════════════════════════════════════════
# DATA CLASSES
# ═══════════════════════════════════════════════════════════════

@dataclass
class Candle:
    open: float
    high: float
    low: float
    close: float
    volume: float
    timestamp: float = field(default_factory=time.time)

@dataclass
class Signal:
    action: SignalAction
    confidence: float
    reason: str
    strategy: Strategy
    indicators: Dict[str, float] = field(default_factory=dict)

@dataclass
class Trade:
    action: str  # BUY or SELL
    asset: str
    price: float
    amount: float
    value: float
    reason: str
    confidence: float
    strategy: str
    timestamp: float = field(default_factory=time.time)
    profit: Optional[float] = None

@dataclass
class Position:
    asset: str
    size: float = 0.0
    avg_entry: float = 0.0
    unrealized_pnl: float = 0.0
    params_at_entry: Optional[Dict[str, float]] = None
    realized_pnl: float = 0.0  # Accumulated profit across partial sells of this position

    def update_pnl(self, current_price: float):
        if self.size > 0:
            self.unrealized_pnl = (current_price - self.avg_entry) * self.size
        else:
            self.unrealized_pnl = 0.0

@dataclass
class PortfolioState:
    balance: float
    position: Position
    equity: float
    pnl_pct: float
    max_drawdown: float
    peak_equity: float
    win_count: int
    loss_count: int
    total_trades: int
    sharpe: float
    regime: Regime
    strategy: Strategy
    signal: Signal
    tick_count: int


# ═══════════════════════════════════════════════════════════════
# INDICATORS (Pure Python, no pandas/numpy)
# ═══════════════════════════════════════════════════════════════

class Indicators:
    """All indicator calculations. Input: list of floats. Output: float."""

    @staticmethod
    def ema(prices: List[float], period: int) -> float:
        """Exponential Moving Average."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        k = 2.0 / (period + 1)
        ema_val = sum(prices[:period]) / period
        for i in range(period, len(prices)):
            ema_val = prices[i] * k + ema_val * (1 - k)
        return ema_val

    @staticmethod
    def sma(prices: List[float], period: int) -> float:
        """Simple Moving Average."""
        if len(prices) < period:
            return prices[-1] if prices else 0.0
        return sum(prices[-period:]) / period

    @staticmethod
    def rsi(prices: List[float], period: int = 14) -> float:
        """Relative Strength Index (0–100) using Wilder's exponential smoothing."""
        if len(prices) < period + 1:
            return 50.0
        # Seed with SMA of first `period` changes
        avg_gain = 0.0
        avg_loss = 0.0
        for i in range(1, period + 1):
            diff = prices[i] - prices[i - 1]
            if diff > 0:
                avg_gain += diff
            else:
                avg_loss -= diff
        avg_gain /= period
        avg_loss /= period
        # Wilder's exponential smoothing for remaining prices
        for i in range(period + 1, len(prices)):
            diff = prices[i] - prices[i - 1]
            gain = diff if diff > 0 else 0.0
            loss = -diff if diff < 0 else 0.0
            avg_gain = (avg_gain * (period - 1) + gain) / period
            avg_loss = (avg_loss * (period - 1) + loss) / period
        if avg_loss == 0:
            return 100.0 if avg_gain > 0 else 50.0
        rs = avg_gain / avg_loss
        return 100.0 - 100.0 / (1.0 + rs)

    @staticmethod
    def atr(candles: List[Candle], period: int = 14) -> float:
        """Average True Range."""
        if len(candles) < period + 1:
            return 0.0
        tr_sum = 0.0
        for i in range(len(candles) - period, len(candles)):
            tr = max(
                candles[i].high - candles[i].low,
                abs(candles[i].high - candles[i - 1].close),
                abs(candles[i].low - candles[i - 1].close),
            )
            tr_sum += tr
        return tr_sum / period

    @staticmethod
    def bollinger_bands(
        prices: List[float], period: int = 20, std_mult: float = 2.0
    ) -> Dict[str, float]:
        """Bollinger Bands: upper, middle, lower, width."""
        if len(prices) < period:
            p = prices[-1] if prices else 0.0
            return {"upper": p, "middle": p, "lower": p, "width": 0.0}
        sl = prices[-period:]
        mean = sum(sl) / period
        variance = sum((x - mean) ** 2 for x in sl) / period
        std = math.sqrt(variance)
        upper = mean + std_mult * std
        lower = mean - std_mult * std
        width = (std_mult * 2 * std) / mean if mean > 0 else 0.0
        return {"upper": upper, "middle": mean, "lower": lower, "width": width}

    @staticmethod
    def macd(
        prices: List[float], fast: int = 12, slow: int = 26, signal_period: int = 9
    ) -> Dict[str, float]:
        """MACD: macd_line, signal_line, histogram."""
        if len(prices) < slow:
            return {"macd": 0.0, "signal": 0.0, "histogram": 0.0}
        # Build historical MACD series by computing EMA-fast minus EMA-slow at each point
        k_fast = 2.0 / (fast + 1)
        k_slow = 2.0 / (slow + 1)
        ema_f = sum(prices[:fast]) / fast
        ema_s = sum(prices[:slow]) / slow
        # Advance fast EMA to slow start point
        for i in range(fast, slow):
            ema_f = prices[i] * k_fast + ema_f * (1 - k_fast)
        macd_hist = []
        for i in range(slow, len(prices)):
            ema_f = prices[i] * k_fast + ema_f * (1 - k_fast)
            ema_s = prices[i] * k_slow + ema_s * (1 - k_slow)
            macd_hist.append(ema_f - ema_s)
        macd_line = macd_hist[-1] if macd_hist else 0.0
        # Signal line = EMA of MACD series
        if len(macd_hist) >= signal_period:
            k_sig = 2.0 / (signal_period + 1)
            sig = sum(macd_hist[:signal_period]) / signal_period
            for i in range(signal_period, len(macd_hist)):
                sig = macd_hist[i] * k_sig + sig * (1 - k_sig)
            signal_line = sig
        else:
            signal_line = macd_line
        histogram = macd_line - signal_line
        return {"macd": macd_line, "signal": signal_line, "histogram": histogram}


# ═══════════════════════════════════════════════════════════════
# REGIME DETECTOR
# ═══════════════════════════════════════════════════════════════

class RegimeDetector:
    """Detects market regime from indicator values."""

    @staticmethod
    def detect(candles: List[Candle], prices: List[float],
               volatile_atr_pct: float = 4.0, volatile_bb_width: float = 0.08,
               trend_ema_ratio: float = 1.005) -> Regime:
        if len(prices) < 50:
            return Regime.RANGING

        ema20 = Indicators.ema(prices, 20)
        ema50 = Indicators.ema(prices, 50)
        atr = Indicators.atr(candles)
        bb = Indicators.bollinger_bands(prices)
        current = prices[-1]
        atr_pct = (atr / current) * 100 if current > 0 else 0

        # High volatility overrides trend detection
        if atr_pct > volatile_atr_pct or bb["width"] > volatile_bb_width:
            return Regime.VOLATILE

        # Trend detection with tunable threshold
        down_ratio = 2.0 - trend_ema_ratio  # mirror: 1.005 → 0.995
        if ema20 > ema50 * trend_ema_ratio and current > ema20:
            return Regime.TREND_UP
        if ema20 < ema50 * down_ratio and current < ema20:
            return Regime.TREND_DOWN

        return Regime.RANGING


# ═══════════════════════════════════════════════════════════════
# STRATEGY SELECTOR
# ═══════════════════════════════════════════════════════════════

REGIME_STRATEGY_MAP = {
    Regime.TREND_UP: Strategy.MOMENTUM,
    Regime.TREND_DOWN: Strategy.DEFENSIVE,
    Regime.RANGING: Strategy.MEAN_REVERSION,
    Regime.VOLATILE: Strategy.GRID,
}


# ═══════════════════════════════════════════════════════════════
# SIGNAL GENERATOR
# ═══════════════════════════════════════════════════════════════

def _fmt_price(p: float) -> str:
    """Format a price for human-readable signal reasons.
    Uses full precision for small prices (e.g. SOL/XBT at 0.0012)."""
    if p < 0.01:
        return f"{p:.6f}"
    if p < 1:
        return f"{p:.4f}"
    return f"{p:.0f}"


class SignalGenerator:
    """Generates BUY/SELL/HOLD signals based on active strategy."""

    @staticmethod
    def generate(
        strategy: Strategy, prices: List[float], candles: List[Candle],
        momentum_rsi_lower: float = 30.0, momentum_rsi_upper: float = 70.0,
        mean_reversion_rsi_buy: float = 35.0, mean_reversion_rsi_sell: float = 65.0,
    ) -> Signal:
        if len(prices) < 26:
            return Signal(
                action=SignalAction.HOLD,
                confidence=0.0,
                reason="Insufficient data — warming up indicators",
                strategy=strategy,
            )

        rsi = Indicators.rsi(prices)
        macd = Indicators.macd(prices)
        bb = Indicators.bollinger_bands(prices)
        current = prices[-1]
        # Use full precision for small-price pairs (e.g. SOL/XBT at 0.0012)
        price_decimals = 8 if current < 1 else 2
        indicators = {
            "rsi": round(rsi, 2),
            "macd_line": round(macd["macd"], 8),
            "macd_signal": round(macd["signal"], 8),
            "macd_histogram": round(macd["histogram"], 8),
            "bb_upper": round(bb["upper"], price_decimals),
            "bb_middle": round(bb["middle"], price_decimals),
            "bb_lower": round(bb["lower"], price_decimals),
            "bb_width": round(bb["width"], 6),
            "price": round(current, price_decimals),
        }

        if strategy == Strategy.MOMENTUM:
            return SignalGenerator._momentum(rsi, macd, bb, current, indicators,
                                             rsi_lower=momentum_rsi_lower,
                                             rsi_upper=momentum_rsi_upper)
        elif strategy == Strategy.MEAN_REVERSION:
            return SignalGenerator._mean_reversion(rsi, bb, current, indicators,
                                                   rsi_buy=mean_reversion_rsi_buy,
                                                   rsi_sell=mean_reversion_rsi_sell)
        elif strategy == Strategy.GRID:
            return SignalGenerator._grid(bb, current, indicators)
        elif strategy == Strategy.DEFENSIVE:
            return SignalGenerator._defensive(rsi, current, indicators)
        else:
            return Signal(
                action=SignalAction.HOLD,
                confidence=0.5,
                reason="Unknown strategy",
                strategy=strategy,
                indicators=indicators,
            )

    @staticmethod
    def _momentum(rsi, macd, bb, price, indicators,
                  rsi_lower: float = 30.0, rsi_upper: float = 70.0) -> Signal:
        if rsi_lower < rsi < rsi_upper and macd["histogram"] > 0 and price > bb["middle"]:
            conf = min(0.95, 0.5 + abs(macd["histogram"]) / price * 1000)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Momentum confirmed: MACD hist {macd['histogram']:.2f} > 0, "
                       f"price {_fmt_price(price)} > BB mid {_fmt_price(bb['middle'])}, RSI {rsi:.1f}",
                strategy=Strategy.MOMENTUM,
                indicators=indicators,
            )
        if rsi > rsi_upper + 5 or macd["histogram"] < 0:
            return Signal(
                action=SignalAction.SELL,
                confidence=0.6,
                reason=f"Momentum fading: RSI {rsi:.1f}" +
                       (f" > 75 overbought" if rsi > 75 else f", MACD crossed negative"),
                strategy=Strategy.MOMENTUM,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=0.5,
            reason=f"Awaiting momentum confirmation (RSI {rsi:.1f}, MACD hist {macd['histogram']:.6f})",
            strategy=Strategy.MOMENTUM,
            indicators=indicators,
        )

    @staticmethod
    def _mean_reversion(rsi, bb, price, indicators,
                        rsi_buy: float = 35.0, rsi_sell: float = 65.0) -> Signal:
        if price <= bb["lower"] and rsi < rsi_buy:
            conf = min(0.9, 0.5 + (bb["middle"] - price) / bb["middle"] * 10)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Mean reversion BUY: price {_fmt_price(price)} at/below BB lower {_fmt_price(bb['lower'])}, RSI {rsi:.1f} oversold",
                strategy=Strategy.MEAN_REVERSION,
                indicators=indicators,
            )
        if price >= bb["upper"] and rsi > rsi_sell:
            conf = min(0.9, 0.5 + (price - bb["middle"]) / bb["middle"] * 10)
            return Signal(
                action=SignalAction.SELL,
                confidence=conf,
                reason=f"Mean reversion SELL: price {_fmt_price(price)} at/above BB upper {_fmt_price(bb['upper'])}, RSI {rsi:.1f} overbought",
                strategy=Strategy.MEAN_REVERSION,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=0.4,
            reason=f"Price {_fmt_price(price)} within bands ({_fmt_price(bb['lower'])}–{_fmt_price(bb['upper'])}), no reversion signal",
            strategy=Strategy.MEAN_REVERSION,
            indicators=indicators,
        )

    @staticmethod
    def _grid(bb, price, indicators) -> Signal:
        grid_spacing = (bb["upper"] - bb["lower"]) / 5 if bb["upper"] != bb["lower"] else 1
        dist_from_lower = (price - bb["lower"]) / grid_spacing if grid_spacing > 0 else 2.5
        if dist_from_lower < 1:
            return Signal(
                action=SignalAction.BUY,
                confidence=0.7,
                reason=f"Grid BUY: price {_fmt_price(price)} in bottom zone (zone {dist_from_lower:.1f}/5)",
                strategy=Strategy.GRID,
                indicators=indicators,
            )
        if dist_from_lower > 4:
            return Signal(
                action=SignalAction.SELL,
                confidence=0.7,
                reason=f"Grid SELL: price {_fmt_price(price)} in top zone (zone {dist_from_lower:.1f}/5)",
                strategy=Strategy.GRID,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=0.3,
            reason=f"Grid HOLD: price in neutral zone {dist_from_lower:.1f}/5",
            strategy=Strategy.GRID,
            indicators=indicators,
        )

    @staticmethod
    def _defensive(rsi, price, indicators) -> Signal:
        if rsi < 20:
            return Signal(
                action=SignalAction.BUY,
                confidence=0.4,
                reason=f"Defensive: extreme oversold RSI {rsi:.1f} — cautious nibble",
                strategy=Strategy.DEFENSIVE,
                indicators=indicators,
            )
        if rsi > 50:
            return Signal(
                action=SignalAction.SELL,
                confidence=0.8,
                reason=f"Defensive: RSI {rsi:.1f} > 50 in downtrend — reducing exposure",
                strategy=Strategy.DEFENSIVE,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=0.6,
            reason=f"Defensive HOLD: preserving capital (RSI {rsi:.1f})",
            strategy=Strategy.DEFENSIVE,
            indicators=indicators,
        )


# ═══════════════════════════════════════════════════════════════
# POSITION SIZER (Quarter-Kelly Criterion)
# ═══════════════════════════════════════════════════════════════

# ─── Sizing Presets ───

SIZING_CONSERVATIVE = {
    "kelly_multiplier": 0.25,   # Quarter-Kelly
    "min_confidence": 0.55,
    "max_position_pct": 0.30,
}

SIZING_COMPETITION = {
    "kelly_multiplier": 0.50,   # Half-Kelly — more aggressive
    "min_confidence": 0.50,     # Lower threshold — trades more often
    "max_position_pct": 0.40,   # Larger positions allowed
}


class PositionSizer:
    MIN_TRADE_VALUE = 0.50    # Minimum $0.50 trade (Kraken costmin)

    # Kraken minimum order sizes per base asset
    MIN_ORDER_SIZE = {
        "SOL": 0.02,
        "XBT": 0.00005,
        "BTC": 0.00005,
        "ETH": 0.001,
    }

    def __init__(self, kelly_multiplier: float = 0.25,
                 min_confidence: float = 0.55,
                 max_position_pct: float = 0.30):
        self.kelly_multiplier = kelly_multiplier
        self.min_confidence = min_confidence
        self.max_position_pct = max_position_pct

    def calculate(self, confidence: float, balance: float, price: float,
                  asset: str = "") -> float:
        """Returns position size in asset units using Kelly criterion."""
        if confidence < self.min_confidence or balance < self.MIN_TRADE_VALUE or price <= 0:
            return 0.0

        # Kelly edge estimate scaled by multiplier
        edge = max(0.0, (confidence * 2.0 - 1.0))  # 0 at 50% conf, 1 at 100%
        kelly = edge * self.kelly_multiplier

        position_value = kelly * balance

        # Enforce max position limit
        max_value = balance * self.max_position_pct
        position_value = min(position_value, max_value)

        # Enforce minimum cost
        if position_value < self.MIN_TRADE_VALUE:
            return 0.0

        size = position_value / price

        # Enforce Kraken minimum order sizes
        base_asset = asset.split("/")[0] if "/" in asset else asset
        min_size = self.MIN_ORDER_SIZE.get(base_asset, 0.02)
        if size < min_size:
            return 0.0

        return size


# ═══════════════════════════════════════════════════════════════
# ORDER BOOK ANALYZER
# ═══════════════════════════════════════════════════════════════

class OrderBookAnalyzer:
    """Analyzes order book depth to generate confidence modifiers.

    Parses Kraken depth data (bids/asks arrays), computes volume imbalance,
    spread, wall detection, and a signal-aware confidence modifier.
    """

    # Imbalance thresholds
    BULLISH_THRESHOLD = 1.5   # bid/ask ratio above this = bullish pressure
    BEARISH_THRESHOLD = 0.67  # bid/ask ratio below this = bearish pressure
    WALL_MULTIPLIER = 3.0     # single level > 3x average = wall detected
    MAX_BOOK_MODIFIER = 0.07  # max confidence adjustment from order book

    @staticmethod
    def analyze(depth_data: dict, signal_action: str = "HOLD") -> dict:
        """Analyze order book depth and return metrics with confidence modifier.

        Args:
            depth_data: Raw Kraken depth JSON with 'bids' and 'asks' arrays.
                        Each entry: [price_str, volume_str, timestamp].
            signal_action: Current signal ("BUY", "SELL", or "HOLD") to
                           determine directional modifier.

        Returns:
            dict with bid_volume, ask_volume, imbalance_ratio, spread_bps,
            bid_wall, ask_wall, confidence_modifier.
        """
        result = {
            "bid_volume": 0.0,
            "ask_volume": 0.0,
            "imbalance_ratio": 1.0,
            "spread_bps": 0.0,
            "bid_wall": False,
            "ask_wall": False,
            "confidence_modifier": 0.0,
        }

        # Extract bids and asks from Kraken depth format
        # Kraken returns: {"PAIR": {"bids": [...], "asks": [...]}}
        bids_raw = []
        asks_raw = []

        if isinstance(depth_data, dict):
            # Direct format: {"bids": [...], "asks": [...]}
            if "bids" in depth_data and "asks" in depth_data:
                bids_raw = depth_data["bids"]
                asks_raw = depth_data["asks"]
            else:
                # Nested format: {"XBTUSDC": {"bids": [...], "asks": [...]}}
                for key, val in depth_data.items():
                    if isinstance(val, dict) and "bids" in val and "asks" in val:
                        bids_raw = val["bids"]
                        asks_raw = val["asks"]
                        break

        if not bids_raw or not asks_raw:
            return result

        # Parse top 10 levels: [[price, volume, timestamp], ...]
        bid_levels = []
        for entry in bids_raw[:10]:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                bid_levels.append((float(entry[0]), float(entry[1])))

        ask_levels = []
        for entry in asks_raw[:10]:
            if isinstance(entry, (list, tuple)) and len(entry) >= 2:
                ask_levels.append((float(entry[0]), float(entry[1])))

        if not bid_levels or not ask_levels:
            return result

        # Volume totals
        bid_volumes = [v for _, v in bid_levels]
        ask_volumes = [v for _, v in ask_levels]
        bid_volume = sum(bid_volumes)
        ask_volume = sum(ask_volumes)

        result["bid_volume"] = round(bid_volume, 6)
        result["ask_volume"] = round(ask_volume, 6)

        # Imbalance ratio
        if ask_volume > 0:
            result["imbalance_ratio"] = round(bid_volume / ask_volume, 4)

        # Spread in basis points
        best_bid = bid_levels[0][0]
        best_ask = ask_levels[0][0]
        mid = (best_bid + best_ask) / 2
        if mid > 0:
            result["spread_bps"] = round((best_ask - best_bid) / mid * 10000, 1)

        # Wall detection: any single level > 3x the average
        avg_bid = bid_volume / len(bid_volumes) if bid_volumes else 0
        avg_ask = ask_volume / len(ask_volumes) if ask_volumes else 0
        result["bid_wall"] = any(v > avg_bid * OrderBookAnalyzer.WALL_MULTIPLIER for v in bid_volumes) if avg_bid > 0 else False
        result["ask_wall"] = any(v > avg_ask * OrderBookAnalyzer.WALL_MULTIPLIER for v in ask_volumes) if avg_ask > 0 else False

        # Confidence modifier based on imbalance and signal direction
        # Scales linearly: half of MAX at threshold, full MAX at extreme (ratio 3.0+ / 0.33-)
        ratio = result["imbalance_ratio"]
        modifier = 0.0
        cap = OrderBookAnalyzer.MAX_BOOK_MODIFIER
        half = cap / 2.0
        bull_range = 3.0 - OrderBookAnalyzer.BULLISH_THRESHOLD   # 1.5
        bear_range = OrderBookAnalyzer.BEARISH_THRESHOLD - 0.33  # 0.34

        if signal_action == "BUY":
            if ratio > OrderBookAnalyzer.BULLISH_THRESHOLD:
                # Strong bid support confirms buy
                excess = min(ratio - OrderBookAnalyzer.BULLISH_THRESHOLD, bull_range)
                modifier = min(cap, half + excess / bull_range * half)
            elif ratio < OrderBookAnalyzer.BEARISH_THRESHOLD:
                # Weak bids contradict buy
                excess = min(OrderBookAnalyzer.BEARISH_THRESHOLD - ratio, bear_range)
                modifier = max(-cap, -(half + excess / bear_range * half))
        elif signal_action == "SELL":
            if ratio > OrderBookAnalyzer.BULLISH_THRESHOLD:
                # Strong bids — don't sell into strength (half modifier)
                modifier = -half
            elif ratio < OrderBookAnalyzer.BEARISH_THRESHOLD:
                # Weak bids confirm sell
                excess = min(OrderBookAnalyzer.BEARISH_THRESHOLD - ratio, bear_range)
                modifier = min(cap, half + excess / bear_range * half)
        # HOLD: no modifier

        result["confidence_modifier"] = round(modifier, 4)
        return result


# ═══════════════════════════════════════════════════════════════
# HAMILTON (1989) REGIME-SWITCHING FILTER
# ═══════════════════════════════════════════════════════════════

class RegimeSwitchingFilter:
    """Hamilton (1989) Bayesian filter over hidden regime states.

    Maintains a posterior probability vector `p_t` over the 4 regimes
    (TREND_UP, TREND_DOWN, RANGING, VOLATILE) and updates it each tick
    from an observation likelihood built from engine indicators.

    Update rule (per tick):
        prior_t   = P^T · p_{t-1}          # transition matrix prediction
        lik_t     = L(obs_t | regime)      # Gaussian likelihood per regime
        p_t       = normalize(lik_t ⊙ prior_t)

    The transition matrix `P` is initialised from a flat prior (slightly
    favouring self-persistence) and can be re-seeded from an empirical
    regime history via `seed_transition_matrix`.

    Pure Python — no numpy dependency. Operates on a fixed 4-dim state
    so every update is O(16) arithmetic ops.
    """

    REGIMES = ("TREND_UP", "TREND_DOWN", "RANGING", "VOLATILE")

    # Feature means per regime (atr_pct, ema_ratio, rsi). Seeded so day-one
    # behaviour roughly matches the hard thresholds in RegimeDetector.
    _FEATURE_MEANS = {
        "TREND_UP":   (1.5, 1.010, 60.0),
        "TREND_DOWN": (1.5, 0.990, 40.0),
        "RANGING":    (1.0, 1.000, 50.0),
        "VOLATILE":   (5.0, 1.000, 50.0),
    }
    # Shared diagonal variances (atr_pct, ema_ratio, rsi).
    _FEATURE_VARS = (2.5, 0.000025, 150.0)

    def __init__(self, persistence: float = 0.85):
        self.persistence = persistence
        self.probs: List[float] = [0.25, 0.25, 0.25, 0.25]
        self.P: List[List[float]] = self._build_transition_matrix(persistence)
        self.observations: int = 0

    @staticmethod
    def _build_transition_matrix(p: float) -> List[List[float]]:
        """Flat prior with self-persistence `p` on the diagonal."""
        n = 4
        off = (1.0 - p) / (n - 1)
        return [[p if i == j else off for j in range(n)] for i in range(n)]

    def seed_transition_matrix(self, regime_history: List[str]):
        """Re-estimate P from an observed regime sequence with Laplace smoothing."""
        if len(regime_history) < 2:
            return
        counts = [[1.0] * 4 for _ in range(4)]  # Laplace-smoothed
        idx = {r: i for i, r in enumerate(self.REGIMES)}
        for a, b in zip(regime_history[:-1], regime_history[1:]):
            if a in idx and b in idx:
                counts[idx[a]][idx[b]] += 1.0
        for i in range(4):
            row_sum = sum(counts[i])
            for j in range(4):
                self.P[i][j] = counts[i][j] / row_sum if row_sum > 0 else 0.25

    @classmethod
    def _observation_likelihood(cls, features: Dict[str, float]) -> List[float]:
        """Diagonal Gaussian likelihood per regime. Returns raw (unnormalised) values."""
        atr_pct = float(features.get("atr_pct", 1.0))
        ema_ratio = float(features.get("ema_ratio", 1.0))
        rsi = float(features.get("rsi", 50.0))
        obs = (atr_pct, ema_ratio, rsi)
        var = cls._FEATURE_VARS
        liks: List[float] = []
        for regime in cls.REGIMES:
            mu = cls._FEATURE_MEANS[regime]
            # log-likelihood for numerical stability, then exp at the end
            log_l = 0.0
            for x, m, v in zip(obs, mu, var):
                log_l += -0.5 * ((x - m) ** 2) / v
            liks.append(math.exp(log_l))
        # Guarantee a positive floor so normalisation never degenerates
        liks = [max(l, 1e-12) for l in liks]
        return liks

    def update(self, features: Dict[str, float]) -> List[float]:
        """Run one filter step. `features` dict needs atr_pct, ema_ratio, rsi."""
        # Predict: prior = P^T · probs
        prior = [0.0] * 4
        for j in range(4):
            s = 0.0
            for i in range(4):
                s += self.P[i][j] * self.probs[i]
            prior[j] = s
        # Observe: likelihood per regime
        lik = self._observation_likelihood(features)
        # Posterior ∝ lik ⊙ prior
        post = [lik[i] * prior[i] for i in range(4)]
        total = sum(post)
        if total <= 0:
            post = [0.25, 0.25, 0.25, 0.25]
        else:
            post = [p / total for p in post]
        self.probs = post
        self.observations += 1
        return post

    def argmax_regime(self) -> str:
        """Return the most probable regime as a string."""
        idx = max(range(4), key=lambda i: self.probs[i])
        return self.REGIMES[idx]

    def probs_dict(self) -> Dict[str, float]:
        return {r: round(self.probs[i], 6) for i, r in enumerate(self.REGIMES)}


# ═══════════════════════════════════════════════════════════════
# JOINT-SIGNAL SOLVER (QAOA-inspired Ising cost Hamiltonian)
# ═══════════════════════════════════════════════════════════════

class JointSignalSolver:
    """Cross-pair signal resolver built on an Ising-style cost Hamiltonian.

    Treats the N trading pairs as N spins (long-bias +1, short-bias -1) and
    finds the configuration that minimises

        E(s) = -h · s + γ · sᵀ Σ s

    where `h_i` combines each pair's per-engine signal with its regime-filter
    drift, and `Σ` is the rolling covariance matrix of log-returns. This is
    the classical cost operator used in QAOA/VQE for portfolio optimisation;
    for N=3 pairs the full configuration space is just 2^3 = 8 states, so we
    exact-diagonalise by enumeration in pure Python.

    Outputs per pair:
        - chosen bias (+1 = long / BUY, -1 = short / SELL, 0 = HOLD)
        - derived confidence from the energy gap to the runner-up
        - human-readable reason referencing the covariance and regime drift
    """

    WINDOW = 50                     # candles used for return series
    COVARIANCE_WEIGHT = 0.5         # γ — correlated-exposure penalty
    REGIME_DRIFT_WEIGHT = 0.5       # λ — how strongly regime probs bias h
    GAP_CONFIDENCE_SCALE = 5.0      # how sharply energy gap maps to confidence
    HOLD_GAP_THRESHOLD = 0.02       # tiny gap ⇒ HOLD

    def __init__(
        self,
        pairs: List[str],
        covariance_weight: float = COVARIANCE_WEIGHT,
        regime_drift_weight: float = REGIME_DRIFT_WEIGHT,
    ):
        self.pairs = list(pairs)
        self.covariance_weight = covariance_weight
        self.regime_drift_weight = regime_drift_weight

    # ─── Math helpers ───

    @staticmethod
    def _log_returns(prices: List[float]) -> List[float]:
        out: List[float] = []
        for i in range(1, len(prices)):
            p0, p1 = prices[i - 1], prices[i]
            if p0 > 0 and p1 > 0:
                out.append(math.log(p1 / p0))
        return out

    @staticmethod
    def _covariance(series: List[List[float]]) -> List[List[float]]:
        """Population covariance of N equal-length series. Pure Python."""
        n = len(series)
        if n == 0:
            return []
        k = min(len(s) for s in series)
        if k < 2:
            return [[0.0] * n for _ in range(n)]
        trimmed = [s[-k:] for s in series]
        means = [sum(s) / k for s in trimmed]
        cov = [[0.0] * n for _ in range(n)]
        for i in range(n):
            for j in range(i, n):
                acc = 0.0
                for t in range(k):
                    acc += (trimmed[i][t] - means[i]) * (trimmed[j][t] - means[j])
                v = acc / k
                cov[i][j] = v
                cov[j][i] = v
        return cov

    def _build_signal_vector(self, all_states: Dict[str, dict]) -> List[float]:
        """h_i = sign(action)*confidence + λ*(p_up - p_down) from the regime filter."""
        h: List[float] = []
        for pair in self.pairs:
            state = all_states.get(pair) or {}
            sig = state.get("signal") or {}
            action = sig.get("action", "HOLD")
            conf = float(sig.get("confidence", 0.0))
            base = 0.0
            if action == "BUY":
                base = conf
            elif action == "SELL":
                base = -conf
            drift = 0.0
            probs = state.get("regime_probs")
            if probs:
                p_up = float(probs.get("TREND_UP", 0.0))
                p_dn = float(probs.get("TREND_DOWN", 0.0))
                drift = p_up - p_dn
            h.append(base + self.regime_drift_weight * drift)
        return h

    def _build_returns(self, all_states: Dict[str, dict]) -> List[List[float]]:
        series: List[List[float]] = []
        for pair in self.pairs:
            state = all_states.get(pair) or {}
            candles = state.get("candles") or []
            closes = [float(c.get("c", 0.0)) for c in candles[-self.WINDOW:]]
            series.append(self._log_returns(closes))
        return series

    @staticmethod
    def _energy(s: List[int], h: List[float], cov: List[List[float]], gamma: float) -> float:
        n = len(s)
        lin = -sum(h[i] * s[i] for i in range(n))
        quad = 0.0
        for i in range(n):
            for j in range(n):
                quad += s[i] * cov[i][j] * s[j]
        return lin + gamma * quad

    # ─── Public API ───

    def solve(self, all_states: Dict[str, dict]) -> Dict[str, dict]:
        """Run one joint-signal decision pass. Returns per-pair override dicts."""
        n = len(self.pairs)
        if n == 0:
            return {}

        h = self._build_signal_vector(all_states)
        series = self._build_returns(all_states)
        cov = self._covariance(series) if series else [[0.0] * n for _ in range(n)]

        # Exact enumeration of 2^n spin configurations.
        configs: List[List[int]] = []
        for mask in range(2 ** n):
            configs.append([1 if (mask >> i) & 1 else -1 for i in range(n)])

        energies = [self._energy(s, h, cov, self.covariance_weight) for s in configs]
        order = sorted(range(len(configs)), key=lambda k: energies[k])
        best = configs[order[0]]
        runner_up = configs[order[1]] if len(order) > 1 else best
        e_best = energies[order[0]]
        e_next = energies[order[1]] if len(order) > 1 else e_best
        gap = e_next - e_best  # ≥ 0

        # Map energy gap to confidence in (0,1). Small gap ⇒ low conviction.
        joint_conf = 1.0 - math.exp(-self.GAP_CONFIDENCE_SCALE * max(gap, 0.0))
        joint_conf = max(0.0, min(1.0, joint_conf))

        # Build per-pair overrides only when the joint picture disagrees with
        # the per-pair signal or the gap is large enough to trust.
        overrides: Dict[str, dict] = {}
        for i, pair in enumerate(self.pairs):
            state = all_states.get(pair) or {}
            sig = state.get("signal") or {}
            current_action = sig.get("action", "HOLD")
            current_conf = float(sig.get("confidence", 0.0))

            spin = best[i]
            if gap < self.HOLD_GAP_THRESHOLD:
                joint_action = "HOLD"
            else:
                joint_action = "BUY" if spin > 0 else "SELL"

            # Blend local conviction (|h_i|) with joint conviction
            local = min(1.0, abs(h[i]))
            blended = round(0.5 * local + 0.5 * joint_conf, 4)

            # Covariance-derived reason string
            diag = [cov[j][j] for j in range(n)]
            reason_bits = [
                f"joint_energy={e_best:+.4f}",
                f"gap={gap:.4f}",
                f"cov_diag={[round(d, 6) for d in diag]}",
                f"h={[round(x, 3) for x in h]}",
            ]
            reason = "Joint-signal solver: " + " | ".join(reason_bits)

            # Emit an override only when (a) joint action differs from current,
            # or (b) the blended confidence meaningfully updates current.
            if joint_action != current_action:
                overrides[pair] = {
                    "action": "OVERRIDE",
                    "signal": joint_action,
                    "confidence_adj": blended,
                    "reason": reason,
                    "joint_energy": round(e_best, 6),
                    "energy_gap": round(gap, 6),
                }
            elif joint_action != "HOLD" and abs(blended - current_conf) > 0.05:
                overrides[pair] = {
                    "action": "ADJUST",
                    "signal": joint_action,
                    "confidence_adj": blended,
                    "reason": reason,
                    "joint_energy": round(e_best, 6),
                    "energy_gap": round(gap, 6),
                }

        # Coordinated swap detection: pair `i` goes short while pair `j` goes long
        # AND both are in the SOL/{USDC,XBT} triangle with an existing SOL position.
        sol_usdc_idx = self.pairs.index("SOL/USDC") if "SOL/USDC" in self.pairs else -1
        sol_xbt_idx = self.pairs.index("SOL/XBT") if "SOL/XBT" in self.pairs else -1
        if sol_usdc_idx >= 0 and sol_xbt_idx >= 0:
            if best[sol_usdc_idx] < 0 and best[sol_xbt_idx] > 0:
                sol_state = all_states.get("SOL/USDC") or {}
                pos = (sol_state.get("position") or {}).get("size", 0.0)
                if pos > 0 and "SOL/USDC" in overrides:
                    overrides["SOL/USDC"]["swap"] = {
                        "sell_pair": "SOL/USDC",
                        "buy_pair": "SOL/XBT",
                        "reason": "Joint-signal: SOL/USDC short-bias + SOL/XBT long-bias ground state",
                    }

        return overrides


# ═══════════════════════════════════════════════════════════════
# CROSS-PAIR REGIME COORDINATOR
# ═══════════════════════════════════════════════════════════════

class CrossPairCoordinator:
    """Joint-signal cross-pair coordinator.

    Replaces the old three-rule if/else with a two-layer pipeline:

        1. Per-pair `RegimeSwitchingFilter` (Hamilton 1989) — turns each
           engine's hard regime label into a probability vector and attaches
           it to the state under `regime_probs`.
        2. `JointSignalSolver` — builds an Ising-style cost Hamiltonian
           `E(s) = -h·s + γ sᵀΣs` over the N-pair spin space, exact-
           diagonalises the 2^N states (8 for the triangle), and emits
           covariance-weighted overrides with confidences derived from the
           energy gap to the runner-up state.

    No hardcoded confidences. All outputs are functions of (h, Σ, p_t).
    """

    HISTORY_SIZE = 50  # larger than the old 10 — needed to seed P

    def __init__(self, pairs: List[str]):
        self.pairs = list(pairs)
        self.regime_history: Dict[str, List[str]] = {p: [] for p in pairs}
        self.filters: Dict[str, RegimeSwitchingFilter] = {
            p: RegimeSwitchingFilter() for p in pairs
        }
        self.solver = JointSignalSolver(pairs)

    def update(self, pair: str, regime: str):
        """Record regime state for a pair. Keeps last HISTORY_SIZE entries.
        Also re-seeds the per-pair transition matrix as history grows."""
        history = self.regime_history.setdefault(pair, [])
        history.append(regime)
        if len(history) > self.HISTORY_SIZE:
            self.regime_history[pair] = history[-self.HISTORY_SIZE:]
        # Re-seed the filter's transition matrix periodically
        if pair in self.filters and len(history) >= 10 and len(history) % 10 == 0:
            self.filters[pair].seed_transition_matrix(self.regime_history[pair])

    def get_overrides(self, all_states: Dict[str, dict]) -> Dict[str, dict]:
        """Run the Hamilton filter then the joint-signal solver across all pairs.

        Mutates each per-pair state dict to attach `regime_probs` (the Hamilton
        posterior) before solving. Returns per-pair overrides in the same
        shape the old rule-based coordinator used, so downstream consumers in
        hydra_agent.py do not need changes.
        """
        # Phase 1: Hamilton filter per pair → attach regime_probs
        for pair in self.pairs:
            state = all_states.get(pair)
            if not state:
                continue
            # Ensure the filter exists for pairs created after construction
            if pair not in self.filters:
                self.filters[pair] = RegimeSwitchingFilter()
            features = self._extract_features(state)
            self.filters[pair].update(features)
            state["regime_probs"] = self.filters[pair].probs_dict()

        # Phase 2: joint-signal solver over the N-pair Ising Hamiltonian
        return self.solver.solve(all_states)

    @staticmethod
    def _extract_features(state: Dict[str, Any]) -> Dict[str, float]:
        """Pull Hamilton-filter observation features from an engine state dict."""
        volatility = state.get("volatility") or {}
        trend = state.get("trend") or {}
        indicators = state.get("indicators") or {}
        ema20 = float(trend.get("ema20", 0.0) or 0.0)
        ema50 = float(trend.get("ema50", 0.0) or 0.0)
        ema_ratio = (ema20 / ema50) if ema50 > 0 else 1.0
        return {
            "atr_pct": float(volatility.get("atr_pct", 0.0) or 0.0),
            "ema_ratio": ema_ratio,
            "rsi": float(indicators.get("rsi", 50.0) or 50.0),
        }


# ═══════════════════════════════════════════════════════════════
# HYDRA ENGINE (Main orchestrator)
# ═══════════════════════════════════════════════════════════════

class HydraEngine:
    """
    Main engine. Ingest candles, get back regime/strategy/signal/trade decisions.

    Usage:
        engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
        engine.ingest_candle({"open": 95000, "high": 95500, "low": 94500, "close": 95200, "volume": 150})
        state = engine.tick()
    """

    MAX_CANDLES = 250
    CIRCUIT_BREAKER_PCT = 15.0  # Stop if drawdown exceeds 15%

    def __init__(self, initial_balance: float = 10000.0, asset: str = "BTC/USD",
                 sizing: Optional[Dict[str, float]] = None,
                 candle_interval: int = 5,
                 volatile_atr_pct: float = 4.0,
                 volatile_bb_width: float = 0.08,
                 trend_ema_ratio: float = 1.005,
                 momentum_rsi_lower: float = 30.0,
                 momentum_rsi_upper: float = 70.0,
                 mean_reversion_rsi_buy: float = 35.0,
                 mean_reversion_rsi_sell: float = 65.0):
        self.asset = asset
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.position = Position(asset=asset)
        cfg = sizing or SIZING_CONSERVATIVE
        self.sizer = PositionSizer(**cfg)
        self.candle_interval = candle_interval
        self.volatile_atr_pct = volatile_atr_pct
        self.volatile_bb_width = volatile_bb_width
        self.trend_ema_ratio = trend_ema_ratio
        self.momentum_rsi_lower = momentum_rsi_lower
        self.momentum_rsi_upper = momentum_rsi_upper
        self.mean_reversion_rsi_buy = mean_reversion_rsi_buy
        self.mean_reversion_rsi_sell = mean_reversion_rsi_sell
        self.candles: List[Candle] = []
        self.prices: List[float] = []
        self.trades: List[Trade] = []
        self.equity_history: List[float] = []
        self.peak_equity = initial_balance
        self.max_drawdown = 0.0
        self.win_count = 0
        self.loss_count = 0
        self.total_trades = 0
        self.tick_count = 0
        self.halted = False
        self.halt_reason = ""

    def ingest_candle(self, raw: Dict[str, Any]) -> None:
        """Add a candle from kraken ohlc JSON output. Deduplicates by timestamp."""
        has_timestamp = "timestamp" in raw
        candle = Candle(
            open=float(raw.get("open", 0)),
            high=float(raw.get("high", 0)),
            low=float(raw.get("low", 0)),
            close=float(raw.get("close", 0)),
            volume=float(raw.get("volume", 0)),
            timestamp=float(raw.get("timestamp", time.time())),
        )
        # Deduplicate: if Kraken timestamp matches last candle, update in place (incomplete candle refresh)
        if has_timestamp and self.candles and self.candles[-1].timestamp == candle.timestamp:
            self.candles[-1] = candle
            self.prices[-1] = candle.close
            return
        self.candles.append(candle)
        self.prices.append(candle.close)
        # Keep memory bounded
        if len(self.candles) > self.MAX_CANDLES:
            self.candles = self.candles[-self.MAX_CANDLES:]
            self.prices = self.prices[-self.MAX_CANDLES:]

    def tick(self, generate_only: bool = False) -> Dict[str, Any]:
        """Run one decision cycle. Returns full state as dict.

        Args:
            generate_only: If True, generate signal but do NOT execute trades.
                           Use execute_signal() afterward to execute selectively.
                           This allows an external layer (e.g. AI brain) to review
                           the signal before committing to a trade.
        """
        self.tick_count += 1

        if self.halted:
            return self._build_state(
                Regime.VOLATILE,
                Strategy.DEFENSIVE,
                Signal(SignalAction.HOLD, 0.0, self.halt_reason, Strategy.DEFENSIVE),
            )

        # Detect regime
        regime = RegimeDetector.detect(
            self.candles, self.prices,
            self.volatile_atr_pct, self.volatile_bb_width, self.trend_ema_ratio,
        )
        strategy = REGIME_STRATEGY_MAP[regime]

        # Generate signal
        signal = SignalGenerator.generate(
            strategy, self.prices, self.candles,
            momentum_rsi_lower=self.momentum_rsi_lower,
            momentum_rsi_upper=self.momentum_rsi_upper,
            mean_reversion_rsi_buy=self.mean_reversion_rsi_buy,
            mean_reversion_rsi_sell=self.mean_reversion_rsi_sell,
        )

        # Execute if actionable (skip when generate_only for external review)
        trade = None if generate_only else self._maybe_execute(signal)

        # Update portfolio metrics
        current_price = self.prices[-1] if self.prices else 0
        self.position.update_pnl(current_price)
        equity = self.balance + (self.position.size * current_price)
        self.equity_history.append(equity)

        # Track drawdown
        if equity > self.peak_equity:
            self.peak_equity = equity
        drawdown = ((self.peak_equity - equity) / self.peak_equity * 100) if self.peak_equity > 0 else 0
        if drawdown > self.max_drawdown:
            self.max_drawdown = drawdown

        # Circuit breaker
        if self.max_drawdown > self.CIRCUIT_BREAKER_PCT:
            self.halted = True
            self.halt_reason = f"CIRCUIT BREAKER: drawdown {self.max_drawdown:.1f}% > {self.CIRCUIT_BREAKER_PCT}% limit"

        return self._build_state(regime, strategy, signal, trade)

    def _maybe_execute(self, signal: Signal) -> Optional[Trade]:
        """Execute trade if signal is actionable."""
        if not self.prices:
            return None

        current_price = self.prices[-1]

        if signal.action == SignalAction.BUY and signal.confidence >= self.sizer.min_confidence:
            size = self.sizer.calculate(signal.confidence, self.balance, current_price, self.asset)
            if size > 0:
                cost = size * current_price
                # Update position (average in)
                if self.position.size > 0:
                    total_size = self.position.size + size
                    self.position.avg_entry = (
                        self.position.avg_entry * self.position.size + current_price * size
                    ) / total_size
                    self.position.size = total_size
                else:
                    self.position.size = size
                    self.position.avg_entry = current_price
                    # Snapshot tunable params at entry for self-tuning
                    self.position.params_at_entry = self.snapshot_params()

                self.balance -= cost
                self.total_trades += 1

                trade = Trade(
                    action="BUY",
                    asset=self.asset,
                    price=current_price,
                    amount=size,
                    value=cost,
                    reason=signal.reason,
                    confidence=signal.confidence,
                    strategy=signal.strategy.value,
                )
                self.trades.append(trade)
                return trade

        elif signal.action == SignalAction.SELL and self.position.size > 0:
            sell_pct = 1.0 if signal.confidence > 0.7 else 0.5
            sell_amount = self.position.size * sell_pct
            revenue = sell_amount * current_price
            profit = (current_price - self.position.avg_entry) * sell_amount

            self.balance += revenue
            self.position.size -= sell_amount
            self.position.realized_pnl += profit
            if self.position.size < 0.00001:
                self.position.size = 0.0
                self.position.avg_entry = 0.0
                # Only count as a completed trade when position is fully closed.
                # Use accumulated realized PnL so partial sells at different
                # confidence levels are tallied correctly (previously only the
                # final leg's profit was used to decide win vs loss).
                total_profit = self.position.realized_pnl
                self.total_trades += 1
                if total_profit > 0:
                    self.win_count += 1
                elif total_profit < 0:
                    self.loss_count += 1
                # total_profit == 0 is break-even, don't count as win or loss
                self.position.params_at_entry = None
                self.position.realized_pnl = 0.0

            trade = Trade(
                action="SELL",
                asset=self.asset,
                price=current_price,
                amount=sell_amount,
                value=revenue,
                reason=signal.reason,
                confidence=signal.confidence,
                strategy=signal.strategy.value,
                profit=profit,
            )
            self.trades.append(trade)
            return trade

        return None

    def execute_signal(self, action: str, confidence: float, reason: str = "",
                        strategy: str = "MOMENTUM") -> Optional[Trade]:
        """Execute a trade based on an externally-provided signal.

        Use after tick(generate_only=True) to execute with a (possibly modified)
        signal from an AI brain or cross-pair coordinator.

        Args:
            action: "BUY", "SELL", or "HOLD"
            confidence: Signal confidence 0-1
            reason: Human-readable reason string
            strategy: Strategy name for logging

        Returns:
            Trade if executed, None otherwise
        """
        try:
            sig_action = SignalAction(action)
        except ValueError:
            return None
        try:
            sig_strategy = Strategy(strategy)
        except ValueError:
            sig_strategy = Strategy.MOMENTUM

        signal = Signal(
            action=sig_action,
            confidence=confidence,
            reason=reason,
            strategy=sig_strategy,
        )
        return self._maybe_execute(signal)

    def snapshot_params(self) -> Dict[str, float]:
        """Return a snapshot of the current tunable parameters."""
        return {
            "volatile_atr_pct": self.volatile_atr_pct,
            "volatile_bb_width": self.volatile_bb_width,
            "trend_ema_ratio": self.trend_ema_ratio,
            "momentum_rsi_lower": self.momentum_rsi_lower,
            "momentum_rsi_upper": self.momentum_rsi_upper,
            "mean_reversion_rsi_buy": self.mean_reversion_rsi_buy,
            "mean_reversion_rsi_sell": self.mean_reversion_rsi_sell,
            "min_confidence_threshold": self.sizer.min_confidence,
        }

    def snapshot_runtime(self) -> Dict[str, Any]:
        """Return a JSON-serialisable snapshot of the engine's mutable runtime
        state. Paired with `restore_runtime` to support `--resume`."""
        return {
            "asset": self.asset,
            "initial_balance": self.initial_balance,
            "balance": self.balance,
            "position": {
                "asset": self.position.asset,
                "size": self.position.size,
                "avg_entry": self.position.avg_entry,
                "unrealized_pnl": self.position.unrealized_pnl,
                "realized_pnl": self.position.realized_pnl,
                "params_at_entry": self.position.params_at_entry,
            },
            "peak_equity": self.peak_equity,
            "max_drawdown": self.max_drawdown,
            "win_count": self.win_count,
            "loss_count": self.loss_count,
            "total_trades": self.total_trades,
            "tick_count": self.tick_count,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "equity_history": list(self.equity_history)[-500:],
            "candles": [
                {
                    "open": c.open, "high": c.high, "low": c.low,
                    "close": c.close, "volume": c.volume, "timestamp": c.timestamp,
                }
                for c in self.candles[-self.MAX_CANDLES:]
            ],
        }

    def restore_runtime(self, snapshot: Dict[str, Any]):
        """Restore mutable state from a snapshot produced by `snapshot_runtime`."""
        if not snapshot:
            return
        self.balance = float(snapshot.get("balance", self.balance))
        self.initial_balance = float(snapshot.get("initial_balance", self.initial_balance))
        pos = snapshot.get("position") or {}
        self.position = Position(
            asset=pos.get("asset", self.asset),
            size=float(pos.get("size", 0.0)),
            avg_entry=float(pos.get("avg_entry", 0.0)),
            unrealized_pnl=float(pos.get("unrealized_pnl", 0.0)),
            params_at_entry=pos.get("params_at_entry"),
            realized_pnl=float(pos.get("realized_pnl", 0.0)),
        )
        self.peak_equity = float(snapshot.get("peak_equity", self.balance))
        self.max_drawdown = float(snapshot.get("max_drawdown", 0.0))
        self.win_count = int(snapshot.get("win_count", 0))
        self.loss_count = int(snapshot.get("loss_count", 0))
        self.total_trades = int(snapshot.get("total_trades", 0))
        self.tick_count = int(snapshot.get("tick_count", 0))
        self.halted = bool(snapshot.get("halted", False))
        self.halt_reason = str(snapshot.get("halt_reason", ""))
        self.equity_history = list(snapshot.get("equity_history", []))
        self.candles = []
        self.prices = []
        for c in snapshot.get("candles", []):
            candle = Candle(
                open=float(c["open"]), high=float(c["high"]),
                low=float(c["low"]), close=float(c["close"]),
                volume=float(c.get("volume", 0.0)),
                timestamp=float(c.get("timestamp", time.time())),
            )
            self.candles.append(candle)
            self.prices.append(candle.close)

    def apply_tuned_params(self, params: Dict[str, float]):
        """Apply tuned parameters from ParameterTracker."""
        if "volatile_atr_pct" in params:
            self.volatile_atr_pct = params["volatile_atr_pct"]
        if "volatile_bb_width" in params:
            self.volatile_bb_width = params["volatile_bb_width"]
        if "trend_ema_ratio" in params:
            self.trend_ema_ratio = params["trend_ema_ratio"]
        if "momentum_rsi_lower" in params:
            self.momentum_rsi_lower = params["momentum_rsi_lower"]
        if "momentum_rsi_upper" in params:
            self.momentum_rsi_upper = params["momentum_rsi_upper"]
        if "mean_reversion_rsi_buy" in params:
            self.mean_reversion_rsi_buy = params["mean_reversion_rsi_buy"]
        if "mean_reversion_rsi_sell" in params:
            self.mean_reversion_rsi_sell = params["mean_reversion_rsi_sell"]
        if "min_confidence_threshold" in params:
            self.sizer.min_confidence = params["min_confidence_threshold"]

    def _candle_status(self) -> str:
        """Check if the latest candle is still forming or closed."""
        if not self.candles:
            return "unknown"
        age = time.time() - self.candles[-1].timestamp
        if age < self.candle_interval * 60:
            return "forming"
        return "closed"

    def _build_state(
        self,
        regime: Regime,
        strategy: Strategy,
        signal: Signal,
        trade: Optional[Trade] = None,
    ) -> Dict[str, Any]:
        """Build complete state dictionary for reporting."""
        current_price = self.prices[-1] if self.prices else 0
        equity = self.balance + (self.position.size * current_price)
        pnl_pct = ((equity - self.initial_balance) / self.initial_balance * 100) if self.initial_balance > 0 else 0
        win_rate = (self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0

        # Sharpe estimate from equity curve
        sharpe = self._calc_sharpe()

        # Trend & volatility (same indicators RegimeDetector uses, surfaced for AI agents)
        atr_val = Indicators.atr(self.candles) if len(self.candles) > 14 else 0.0
        ema20 = Indicators.ema(self.prices, 20) if len(self.prices) >= 20 else current_price
        ema50 = Indicators.ema(self.prices, 50) if len(self.prices) >= 50 else current_price
        atr_pct = (atr_val / current_price * 100) if current_price > 0 else 0.0

        # Volume stats
        vol_current = self.candles[-1].volume if self.candles else 0.0
        vol_window = self.candles[-20:] if self.candles else []
        vol_avg = (sum(c.volume for c in vol_window) / len(vol_window)) if vol_window else 0.0

        state = {
            "tick": self.tick_count,
            "timestamp": time.time(),
            "asset": self.asset,
            "price": round(current_price, 8),
            "regime": regime.value,
            "strategy": strategy.value,
            "signal": {
                "action": signal.action.value,
                "confidence": round(signal.confidence, 4),
                "reason": signal.reason,
            },
            "position": {
                "size": round(self.position.size, 8),
                "avg_entry": round(self.position.avg_entry, 8),
                "unrealized_pnl": round(self.position.unrealized_pnl, 2),
            },
            "portfolio": {
                "balance": round(self.balance, 2),
                "equity": round(equity, 2),
                "pnl_pct": round(pnl_pct, 4),
                "max_drawdown_pct": round(self.max_drawdown, 4),
                "peak_equity": round(self.peak_equity, 2),
            },
            "performance": {
                "total_trades": self.total_trades,
                "win_count": self.win_count,
                "loss_count": self.loss_count,
                "win_rate_pct": round(win_rate, 2),
                "sharpe_estimate": round(sharpe, 4),
            },
            "trend": {
                "ema20": round(ema20, 8),
                "ema50": round(ema50, 8),
            },
            "volatility": {
                "atr": round(atr_val, 8),
                "atr_pct": round(atr_pct, 4),
            },
            "volume": {
                "current": round(vol_current, 4),
                "avg_20": round(vol_avg, 4),
            },
            "candle_interval": self.candle_interval,
            "candle_status": self._candle_status(),
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "indicators": signal.indicators if signal.indicators else {},
            "candles": [
                {"o": c.open, "h": c.high, "l": c.low, "c": c.close, "t": c.timestamp}
                for c in self.candles[-100:]
            ],
            # Populated by CrossPairCoordinator's Hamilton filter on Phase 1.5.
            # Present as an empty dict on unfiltered ticks so consumers can
            # unconditionally introspect it without a key check.
            "regime_probs": {},
        }

        if trade:
            state["last_trade"] = {
                "action": trade.action,
                "price": round(trade.price, 2),
                "amount": round(trade.amount, 8),
                "value": round(trade.value, 2),
                "reason": trade.reason,
                "profit": round(trade.profit, 2) if trade.profit is not None else None,
            }

        return state

    def _calc_sharpe(self) -> float:
        """Estimate Sharpe ratio from equity history.

        Annualisation is derived from the *observed* candle timestamp deltas,
        not from the nominal `candle_interval` argument, so mismatches
        between the configured interval and the exchange's actual candle
        cadence do not skew the result.
        """
        if len(self.equity_history) < 30:
            return 0.0
        recent = self.equity_history[-60:]
        returns = [
            (recent[i] - recent[i - 1]) / recent[i - 1]
            for i in range(1, len(recent))
            if recent[i - 1] > 0
        ]
        if len(returns) < 2:
            return 0.0
        avg = sum(returns) / len(returns)
        var = sum((r - avg) ** 2 for r in returns) / (len(returns) - 1)
        std = math.sqrt(var) if var > 0 else 1.0

        # Observed period length in seconds — median of recent candle deltas.
        # Falls back to the nominal `candle_interval` when the observed
        # cadence is clearly synthetic (sub-second) or unavailable.
        period_seconds = 0.0
        if len(self.candles) >= 3:
            deltas = [
                self.candles[i].timestamp - self.candles[i - 1].timestamp
                for i in range(1, len(self.candles))
                if self.candles[i].timestamp > self.candles[i - 1].timestamp
            ]
            if deltas:
                deltas.sort()
                period_seconds = deltas[len(deltas) // 2]
        if period_seconds < 1.0:   # synthetic / unavailable → use nominal
            period_seconds = float(self.candle_interval) * 60.0
        periods_per_year = (365.25 * 24.0 * 3600.0) / period_seconds
        return (avg / std) * math.sqrt(periods_per_year) if std > 0 else 0.0

    def get_performance_report(self) -> str:
        """Generate a formatted performance report."""
        if not self.prices:
            return "No data yet."

        current_price = self.prices[-1]
        equity = self.balance + self.position.size * current_price
        pnl = equity - self.initial_balance
        pnl_pct = (pnl / self.initial_balance) * 100
        win_rate = (self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0

        gross_profit = sum(t.profit for t in self.trades if t.profit and t.profit > 0)
        gross_loss = abs(sum(t.profit for t in self.trades if t.profit and t.profit < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        w = 60  # inner width between ║ chars
        def row(label, value):
            content = f"  {label:<18}{value}"
            return f"  {content:<{w}}"
        def sep():
            return "  " + "-" * w

        status = f"HALTED — {self.halt_reason[:40]}" if self.halted else "ACTIVE"
        base = self.asset.split("/")[0]

        lines = [
            "",
            "  " + "=" * w,
            f"  {'HYDRA PERFORMANCE REPORT':^{w}}",
            "  " + "=" * w,
            row("Asset", self.asset),
            row("Duration", f"{self.tick_count} ticks"),
            row("Initial Balance", f"${self.initial_balance:,.2f}"),
            row("Final Balance", f"${equity:,.2f}"),
            sep(),
            row("Net P&L", f"${pnl:+,.2f}  ({pnl_pct:+.2f}%)"),
            row("Max Drawdown", f"{self.max_drawdown:.2f}%"),
            row("Sharpe Ratio", f"{self._calc_sharpe():.4f}"),
            row("Profit Factor", f"{profit_factor:.2f}"),
            sep(),
            row("Total Trades", str(self.total_trades)),
            row("Wins", str(self.win_count)),
            row("Losses", str(self.loss_count)),
            row("Win Rate", f"{win_rate:.1f}%"),
            sep(),
            row("Open Position", f"{self.position.size:.6f} {base}"),
            row("Avg Entry", "$" + _fmt_price(self.position.avg_entry)),
            row("Unrealized P&L", f"${self.position.unrealized_pnl:+,.2f}"),
            row("Cash Balance", f"${self.balance:,.2f}"),
            sep(),
            row("Status", status),
            "  " + "=" * w,
            "",
        ]
        return "\n".join(lines)


# ═══════════════════════════════════════════════════════════════
# CLI ENTRY POINT
# ═══════════════════════════════════════════════════════════════

if __name__ == "__main__":
    # Demo: run with synthetic data
    import random

    engine = HydraEngine(initial_balance=10000, asset="BTC/USD")
    price = 95000.0

    print("HYDRA Engine — Synthetic Demo")
    print("=" * 60)

    for i in range(300):
        # Random walk with slight upward drift
        price *= 1 + random.gauss(0.0001, 0.003)
        candle = {
            "open": price * (1 - random.random() * 0.002),
            "high": price * (1 + random.random() * 0.005),
            "low": price * (1 - random.random() * 0.005),
            "close": price,
            "volume": 50 + random.random() * 200,
        }
        engine.ingest_candle(candle)
        state = engine.tick()

        if i % 30 == 0 and i > 0:
            print(
                f"Tick {state['tick']:>4} | "
                f"${state['price']:>9,.2f} | "
                f"{state['regime']:<10} | "
                f"{state['strategy']:<15} | "
                f"{state['signal']['action']:<4} {state['signal']['confidence']:.2f} | "
                f"Equity: ${state['portfolio']['equity']:>10,.2f} | "
                f"P&L: {state['portfolio']['pnl_pct']:>+.2f}%"
            )

        if state.get("last_trade"):
            t = state["last_trade"]
            print(f"  >>> TRADE: {t['action']} {t['amount']:.6f} @ ${t['price']:,.2f} — {t['reason'][:60]}")

    print()
    print(engine.get_performance_report())
