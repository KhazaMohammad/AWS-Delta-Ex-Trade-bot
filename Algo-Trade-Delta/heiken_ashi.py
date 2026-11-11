"""
Heiken Ashi candle calculations.

Heiken Ashi (HA) candles smooth price action and make trend direction easy to
read. Each HA candle is derived from the standard OHLC of the corresponding
exchange candle:

    HA_Close  = (Open + High + Low + Close) / 4
    HA_Open   = (Previous HA_Open + Previous HA_Close) / 2
    HA_High   = Max(High, HA_Open, HA_Close)
    HA_Low    = Min(Low, HA_Open, HA_Close)

Candle color:
    GREEN if HA_Close >= HA_Open  (bullish)
    RED   if HA_Close <  HA_Open  (bearish)

Only fully closed candles from the exchange are used — never incomplete ones.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional


@dataclass
class Candle:
    """A single OHLCV candle as returned by the exchange."""
    timestamp: str          # ISO8601 UTC (candle start / close time)
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


@dataclass
class HeikenAshiCandle:
    """A single Heiken Ashi candle derived from an OHLC candle."""
    timestamp: str
    ha_open: float
    ha_high: float
    ha_low: float
    ha_close: float

    @property
    def color(self) -> str:
        """'GREEN' if close >= open, else 'RED'."""
        return "GREEN" if self.ha_close >= self.ha_open else "RED"

    @property
    def is_bullish(self) -> bool:
        return self.ha_close >= self.ha_open


def compute_heiken_ashi(
    candles: list[Candle],
    previous_ha: Optional[HeikenAshiCandle] = None,
) -> list[HeikenAshiCandle]:
    """
    Convert a list of OHLC candles into Heiken Ashi candles.

    Parameters
    ----------
    candles : list[Candle]
        Exchange OHLC candles in chronological order (oldest first).
    previous_ha : HeikenAshiCandle | None
        The previous completed HA candle (needed to compute HA_Open of the
        first candle in this batch). If None, HA_Open of the first candle
        falls back to (open + close) / 2.

    Returns
    -------
    list[HeikenAshiCandle]
        HA candles aligned 1:1 with the input candles.
    """
    result: list[HeikenAshiCandle] = []
    prev: Optional[HeikenAshiCandle] = previous_ha

    for i, c in enumerate(candles):
        if i == 0 and prev is None:
            # Seed: no prior HA data available.
            ha_open = (c.open + c.close) / 2.0
        elif i == 0 and prev is not None:
            ha_open = (prev.ha_open + prev.ha_close) / 2.0
        else:
            ha_open = (result[-1].ha_open + result[-1].ha_close) / 2.0

        ha_close = (c.open + c.high + c.low + c.close) / 4.0
        ha_high = max(c.high, ha_open, ha_close)
        ha_low = min(c.low, ha_open, ha_close)

        ha = HeikenAshiCandle(
            timestamp=c.timestamp,
            ha_open=ha_open,
            ha_high=ha_high,
            ha_low=ha_low,
            ha_close=ha_close,
        )
        result.append(ha)
        prev = ha

    return result


def candle_color(candle: HeikenAshiCandle) -> str:
    """Convenience: return 'GREEN' or 'RED' for a HA candle."""
    return candle.color
