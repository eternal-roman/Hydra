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
            return 100.0
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
    def detect(candles: List[Candle], prices: List[float]) -> Regime:
        if len(prices) < 50:
            return Regime.RANGING

        ema20 = Indicators.ema(prices, 20)
        ema50 = Indicators.ema(prices, 50)
        atr = Indicators.atr(candles)
        bb = Indicators.bollinger_bands(prices)
        current = prices[-1]
        atr_pct = (atr / current) * 100 if current > 0 else 0

        # High volatility overrides trend detection
        if atr_pct > 4.0 or bb["width"] > 0.08:
            return Regime.VOLATILE

        # Trend detection with threshold
        if ema20 > ema50 * 1.005 and current > ema20:
            return Regime.TREND_UP
        if ema20 < ema50 * 0.995 and current < ema20:
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

class SignalGenerator:
    """Generates BUY/SELL/HOLD signals based on active strategy."""

    @staticmethod
    def generate(
        strategy: Strategy, prices: List[float], candles: List[Candle]
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
            "macd_histogram": round(macd["histogram"], 8),
            "bb_upper": round(bb["upper"], price_decimals),
            "bb_middle": round(bb["middle"], price_decimals),
            "bb_lower": round(bb["lower"], price_decimals),
            "bb_width": round(bb["width"], 6),
            "price": round(current, price_decimals),
        }

        if strategy == Strategy.MOMENTUM:
            return SignalGenerator._momentum(rsi, macd, bb, current, indicators)
        elif strategy == Strategy.MEAN_REVERSION:
            return SignalGenerator._mean_reversion(rsi, bb, current, indicators)
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
    def _momentum(rsi, macd, bb, price, indicators) -> Signal:
        if 30 < rsi < 70 and macd["histogram"] > 0 and price > bb["middle"]:
            conf = min(0.95, 0.5 + abs(macd["histogram"]) / price * 1000)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Momentum confirmed: MACD hist {macd['histogram']:.2f} > 0, "
                       f"price {price:.0f} > BB mid {bb['middle']:.0f}, RSI {rsi:.1f}",
                strategy=Strategy.MOMENTUM,
                indicators=indicators,
            )
        if rsi > 75 or macd["histogram"] < 0:
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
            reason=f"Awaiting momentum confirmation (RSI {rsi:.1f}, MACD hist {macd['histogram']:.4f})",
            strategy=Strategy.MOMENTUM,
            indicators=indicators,
        )

    @staticmethod
    def _mean_reversion(rsi, bb, price, indicators) -> Signal:
        if price <= bb["lower"] and rsi < 35:
            conf = min(0.9, 0.5 + (bb["middle"] - price) / bb["middle"] * 10)
            return Signal(
                action=SignalAction.BUY,
                confidence=conf,
                reason=f"Mean reversion BUY: price {price:.0f} at/below BB lower {bb['lower']:.0f}, RSI {rsi:.1f} oversold",
                strategy=Strategy.MEAN_REVERSION,
                indicators=indicators,
            )
        if price >= bb["upper"] and rsi > 65:
            conf = min(0.9, 0.5 + (price - bb["middle"]) / bb["middle"] * 10)
            return Signal(
                action=SignalAction.SELL,
                confidence=conf,
                reason=f"Mean reversion SELL: price {price:.0f} at/above BB upper {bb['upper']:.0f}, RSI {rsi:.1f} overbought",
                strategy=Strategy.MEAN_REVERSION,
                indicators=indicators,
            )
        return Signal(
            action=SignalAction.HOLD,
            confidence=0.4,
            reason=f"Price {price:.0f} within bands ({bb['lower']:.0f}–{bb['upper']:.0f}), no reversion signal",
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
                reason=f"Grid BUY: price {price:.0f} in bottom zone (zone {dist_from_lower:.1f}/5)",
                strategy=Strategy.GRID,
                indicators=indicators,
            )
        if dist_from_lower > 4:
            return Signal(
                action=SignalAction.SELL,
                confidence=0.7,
                reason=f"Grid SELL: price {price:.0f} in top zone (zone {dist_from_lower:.1f}/5)",
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

class PositionSizer:
    MAX_POSITION_PCT = 0.30   # Never more than 30% of balance per trade
    BASE_ALLOCATION = 0.10    # Base 10% allocation, scaled by confidence
    MIN_TRADE_VALUE = 0.50    # Minimum $0.50 trade (Kraken costmin)
    MIN_CONFIDENCE = 0.55     # Don't trade below this

    # Kraken minimum order sizes per base asset
    MIN_ORDER_SIZE = {
        "SOL": 0.02,
        "XBT": 0.00005,
        "BTC": 0.00005,
        "ETH": 0.001,
    }

    @staticmethod
    def calculate(confidence: float, balance: float, price: float,
                  asset: str = "") -> float:
        """Returns position size in asset units using modified quarter-Kelly."""
        if confidence < PositionSizer.MIN_CONFIDENCE or balance < PositionSizer.MIN_TRADE_VALUE:
            return 0.0

        # Quarter-Kelly edge estimate: scale allocation by confidence edge
        edge = max(0.0, (confidence * 2.0 - 1.0))  # 0 at 50% conf, 1 at 100%
        kelly_quarter = edge * 0.25

        # Position value = kelly fraction * balance
        # At 0.55 conf: edge=0.1, kelly_q=0.025, value=0.025*bal= $2.50 on $100
        # At 0.95 conf: edge=0.9, kelly_q=0.225, value=0.225*bal= $22.50 on $100
        position_value = kelly_quarter * balance

        # Enforce max position limit
        max_value = balance * PositionSizer.MAX_POSITION_PCT
        position_value = min(position_value, max_value)

        # Enforce minimum cost
        if position_value < PositionSizer.MIN_TRADE_VALUE:
            return 0.0

        size = position_value / price

        # Enforce Kraken minimum order sizes
        base_asset = asset.split("/")[0] if "/" in asset else asset
        min_size = PositionSizer.MIN_ORDER_SIZE.get(base_asset, 0.02)
        if size < min_size:
            return 0.0

        return size


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

    def __init__(self, initial_balance: float = 10000.0, asset: str = "BTC/USD"):
        self.asset = asset
        self.initial_balance = initial_balance
        self.balance = initial_balance
        self.position = Position(asset=asset)
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
        """Add a candle from kraken ohlc JSON output."""
        candle = Candle(
            open=float(raw.get("open", 0)),
            high=float(raw.get("high", 0)),
            low=float(raw.get("low", 0)),
            close=float(raw.get("close", 0)),
            volume=float(raw.get("volume", 0)),
            timestamp=float(raw.get("timestamp", time.time())),
        )
        self.candles.append(candle)
        self.prices.append(candle.close)
        # Keep memory bounded
        if len(self.candles) > self.MAX_CANDLES:
            self.candles = self.candles[-self.MAX_CANDLES:]
            self.prices = self.prices[-self.MAX_CANDLES:]

    def tick(self) -> Dict[str, Any]:
        """Run one decision cycle. Returns full state as dict."""
        self.tick_count += 1

        if self.halted:
            return self._build_state(
                Regime.VOLATILE,
                Strategy.DEFENSIVE,
                Signal(SignalAction.HOLD, 0.0, self.halt_reason, Strategy.DEFENSIVE),
            )

        # Detect regime
        regime = RegimeDetector.detect(self.candles, self.prices)
        strategy = REGIME_STRATEGY_MAP[regime]

        # Generate signal
        signal = SignalGenerator.generate(strategy, self.prices, self.candles)

        # Execute if actionable
        trade = self._maybe_execute(signal)

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

        if signal.action == SignalAction.BUY and signal.confidence >= PositionSizer.MIN_CONFIDENCE:
            size = PositionSizer.calculate(signal.confidence, self.balance, current_price, self.asset)
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
            if self.position.size < 0.00001:
                self.position.size = 0.0
                self.position.avg_entry = 0.0

            self.total_trades += 1
            if profit > 0:
                self.win_count += 1
            else:
                self.loss_count += 1

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
                "avg_entry": round(self.position.avg_entry, 2),
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
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "indicators": signal.indicators if signal.indicators else {},
            "candles": [
                {"o": c.open, "h": c.high, "l": c.low, "c": c.close, "t": c.timestamp}
                for c in self.candles[-100:]
            ],
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
        """Estimate Sharpe ratio from equity history."""
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
        # Annualize (assuming ~1-minute candles, 525600 mins/year)
        return (avg / std) * math.sqrt(525600) if std > 0 else 0.0

    def get_performance_report(self) -> str:
        """Generate a formatted performance report."""
        if not self.prices:
            return "No data yet."

        current_price = self.prices[-1]
        equity = self.balance + self.position.size * current_price
        pnl = equity - self.initial_balance
        pnl_pct = (pnl / self.initial_balance) * 100
        win_rate = (self.win_count / (self.win_count + self.loss_count) * 100) if (self.win_count + self.loss_count) > 0 else 0

        # Profit factor
        gross_profit = sum(t.profit for t in self.trades if t.profit and t.profit > 0)
        gross_loss = abs(sum(t.profit for t in self.trades if t.profit and t.profit < 0))
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float("inf")

        report = f"""
╔══════════════════════════════════════════════════════════════╗
║                  HYDRA PERFORMANCE REPORT                    ║
╠══════════════════════════════════════════════════════════════╣
║  Asset:           {self.asset:<42}║
║  Duration:        {self.tick_count} ticks{' ' * (36 - len(str(self.tick_count)))}║
║  Initial Balance: ${self.initial_balance:>10,.2f}{' ' * 29}║
║  Final Equity:    ${equity:>10,.2f}{' ' * 29}║
╠══════════════════════════════════════════════════════════════╣
║  NET P&L:         ${pnl:>+10,.2f}  ({pnl_pct:>+.2f}%){' ' * (21 - len(f'{pnl_pct:>+.2f}'))}║
║  Max Drawdown:    {self.max_drawdown:>10.2f}%{' ' * 29}║
║  Sharpe Ratio:    {self._calc_sharpe():>10.4f}{' ' * 30}║
║  Profit Factor:   {profit_factor:>10.2f}{' ' * 30}║
╠══════════════════════════════════════════════════════════════╣
║  Total Trades:    {self.total_trades:>10}{' ' * 30}║
║  Wins:            {self.win_count:>10}{' ' * 30}║
║  Losses:          {self.loss_count:>10}{' ' * 30}║
║  Win Rate:        {win_rate:>10.1f}%{' ' * 29}║
╠══════════════════════════════════════════════════════════════╣
║  Open Position:   {self.position.size:>10.6f} {self.asset.split('/')[0]}{' ' * (25 - len(self.asset.split('/')[0]))}║
║  Avg Entry:       ${self.position.avg_entry:>10,.2f}{' ' * 29}║
║  Unrealized P&L:  ${self.position.unrealized_pnl:>+10,.2f}{' ' * 29}║
║  Cash Balance:    ${self.balance:>10,.2f}{' ' * 29}║
╠══════════════════════════════════════════════════════════════╣
║  Status:          {'HALTED — ' + self.halt_reason[:30] if self.halted else 'ACTIVE':<42}║
╚══════════════════════════════════════════════════════════════╝
"""
        return report.strip()


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
