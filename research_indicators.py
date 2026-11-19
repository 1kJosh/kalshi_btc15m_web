"""Research-only BTC indicators compatible with TradingView-style definitions.

No TradingView connection, credentials, orders, or trading decisions are made here.
Supply *completed* OHLCV candles in chronological order. Compare the outputs
against TradingView with the same symbol, exchange, timeframe and candle source.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Sequence


@dataclass(frozen=True)
class Candle:
    high: float
    low: float
    close: float
    volume: float


def ema(values: Sequence[float], period: int) -> float | None:
    """EMA seeded with the SMA of the first period values."""
    if period < 1:
        raise ValueError("period must be positive")
    if len(values) < period:
        return None
    result = sum(values[:period]) / period
    alpha = 2 / (period + 1)
    for value in values[period:]:
        result += alpha * (value - result)
    return result


def rsi(closes: Sequence[float], period: int = 14) -> float | None:
    """Wilder RSI using an initial SMA of gains and losses."""
    if period < 1:
        raise ValueError("period must be positive")
    if len(closes) <= period:
        return None
    deltas = [b - a for a, b in zip(closes, closes[1:])]
    gains = [max(x, 0.0) for x in deltas]
    losses = [max(-x, 0.0) for x in deltas]
    gain = sum(gains[:period]) / period
    loss = sum(losses[:period]) / period
    for g, l in zip(gains[period:], losses[period:]):
        gain = (gain * (period - 1) + g) / period
        loss = (loss * (period - 1) + l) / period
    if loss == 0:
        return 100.0 if gain > 0 else 50.0
    return 100 - 100 / (1 + gain / loss)


def macd(closes: Sequence[float], fast: int = 12, slow: int = 26,
         signal: int = 9) -> tuple[float, float, float] | None:
    """MACD line, signal, histogram; all EMAs use SMA seeds."""
    if not (0 < fast < slow and signal > 0):
        raise ValueError("invalid MACD periods")
    if len(closes) < slow + signal - 1:
        return None
    lines = []
    for end in range(slow, len(closes) + 1):
        short = ema(closes[:end], fast)
        long = ema(closes[:end], slow)
        lines.append(short - long)
    sig = ema(lines, signal)
    return lines[-1], sig, lines[-1] - sig


def session_vwap(candles: Sequence[Candle]) -> float | None:
    """Typical-price VWAP; caller must supply candles from ONE session."""
    if not candles:
        return None
    if any(c.volume < 0 or c.high < c.low for c in candles):
        raise ValueError("invalid candle")
    total_volume = sum(c.volume for c in candles)
    if total_volume <= 0:
        return None
    return sum(((c.high + c.low + c.close) / 3) * c.volume
               for c in candles) / total_volume


def snapshot(candles: Sequence[Candle]) -> dict:
    """Indicator snapshot only; NEVER a buy/sell instruction."""
    closes = [c.close for c in candles]
    return {
        "ema9": ema(closes, 9),
        "ema21": ema(closes, 21),
        "rsi14": rsi(closes, 14),
        "macd_12_26_9": macd(closes),
        "session_vwap": session_vwap(candles),
        "research_only": True,
    }
