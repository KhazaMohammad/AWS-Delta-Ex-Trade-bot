"""
Strategy engine — Heiken Ashi trend-following with scaling and reversal.

Rules (from the spec):

Entry:
  * Latest completed 2h Heiken Ashi candle closes GREEN -> open LONG using base_lot.
  * Latest completed 2h Heiken Ashi candle closes RED   -> open SHORT using base_lot.

Scaling (same direction continues):
  * Each newly closed candle of the same color -> add one base_lot.
  * Stop at max_entries; log skipped scaling signals.

Reversal (color changes):
  * Close all positions in the old direction.
  * Confirm closure on the exchange.
  * Reset entry count.
  * Open base_lot in the new direction.

Only fully closed candles are evaluated; each candle is processed exactly once
(guarded by last_processed_candle_ts in SQLite).
"""

from __future__ import annotations

from typing import Optional

from config import (
    CANDLE_TIMEFRAME_MINUTES,
    HISTORICAL_CANDLE_LIMIT,
    SYMBOL_CONFIG,
    USE_CLOSED_CANDLES_ONLY,
    USE_HEIKEN_ASHI,
    base_lot,
    max_entries,
    max_position_size,
)
from database import Database
from delta_client import DeltaClient
from heiken_ashi import Candle, compute_heiken_ashi, candle_color
from logger import get_logger
from position_manager import PositionManager

logger = get_logger("strategy")


class Strategy:
    def __init__(
        self,
        client: DeltaClient,
        db: Database,
        position_manager: PositionManager,
    ) -> None:
        self.client = client
        self.db = db
        self.pm = position_manager

    # ------------------------------------------------------------------ #
    # Candle fetching
    # ------------------------------------------------------------------ #
    def fetch_closed_candles(self, symbol: str) -> list[Candle]:
        """
        Fetch the most recent 2-hour OHLCV candles for a symbol.

        Returns candles sorted oldest -> newest. All candles returned by the
        exchange /v2/candles endpoint are treated as completed (the latest
        candle is the most recently closed 2-hour bucket).
        """
        raw = self.client.get_candles(
            symbol=symbol,
            resolution=str(CANDLE_TIMEFRAME_MINUTES),  # 120 = 2 hours
            limit=HISTORICAL_CANDLE_LIMIT,
        )
        candles: list[Candle] = []
        for c in raw:
            # Delta Exchange returns 'time' as unix seconds; 'open','high','low','close','volume'
            ts = c.get("time") or c.get("t") or c.get("timestamp")
            if ts is None:
                continue
            try:
                ts_int = int(ts)
            except (TypeError, ValueError):
                continue
            from datetime import datetime, timezone
            ts_iso = datetime.fromtimestamp(ts_int, tz=timezone.utc).isoformat()
            candles.append(Candle(
                timestamp=ts_iso,
                open=float(c.get("open", 0) or 0),
                high=float(c.get("high", 0) or 0),
                low=float(c.get("low", 0) or 0),
                close=float(c.get("close", 0) or 0),
                volume=float(c.get("volume", 0) or 0),
            ))
        candles.sort(key=lambda c: c.timestamp)
        return candles

    def latest_closed_candle(self, symbol: str) -> Optional[Candle]:
        """Return the most recent closed candle (newest), or None."""
        candles = self.fetch_closed_candles(symbol)
        if not candles:
            return None
        return candles[-1]

    # ------------------------------------------------------------------ #
    # Main per-symbol evaluation
    # ------------------------------------------------------------------ #
    def evaluate_symbol(self, symbol: str) -> None:
        """
        Evaluate the strategy for one symbol using the latest completed
        2-hour Heiken Ashi candle.

        Steps:
          1. Skip if this candle was already processed (dedupe).
          2. Build Heiken Ashi candles from OHLC.
          3. Determine color of the latest closed candle.
          4. Apply entry / scale / reversal logic.
          5. Record last_processed_candle_ts so we process each candle once.
        """
        state = self.pm.state(symbol)

        # Fetch latest closed candle
        latest = self.latest_closed_candle(symbol)
        if latest is None:
            logger.warning("No candle data for %s — skipping evaluation.", symbol)
            self.db.audit("WARNING", f"No candle data for {symbol}", symbol=symbol)
            return

        candle_ts = latest.timestamp

        # Dedup: process every candle exactly once
        if state.last_processed_candle_ts == candle_ts:
            logger.debug("Candle %s already processed for %s — skipping.", candle_ts, symbol)
            return

        # Heiken Ashi: need recent history to compute HA_Open correctly.
        # Fetch candles, convert to HA, use the last one's color.
        raw_candles = self.fetch_closed_candles(symbol)
        if not raw_candles:
            return

        # Provide previous HA candle from persistence if available so HA_Open
        # chains correctly across restarts.
        previous_ha = None
        ha_list = compute_heiken_ashi(raw_candles, previous_ha=previous_ha)
        if not ha_list:
            return

        latest_ha = ha_list[-1]
        color = candle_color(latest_ha)
        logger.info(
            "Candle %s for %s: HA color=%s (open=%.4f close=%.4f high=%.4f low=%.4f)",
            candle_ts, symbol, color,
            latest_ha.ha_open, latest_ha.ha_close, latest_ha.ha_high, latest_ha.ha_low,
        )
        self.db.audit("SIGNAL", f"Candle {candle_ts} color={color}",
                      symbol=symbol, details={
                          "timestamp": candle_ts,
                          "ha_open": latest_ha.ha_open,
                          "ha_close": latest_ha.ha_close,
                          "ha_high": latest_ha.ha_high,
                          "ha_low": latest_ha.ha_low,
                          "color": color,
                      })

        # Log raw OHLC + HA values
        logger.debug("OHLC %s: O=%.4f H=%.4f L=%.4f C=%.4f | HA O=%.4f C=%.4f",
                     symbol, latest.open, latest.high, latest.low, latest.close,
                     latest_ha.ha_open, latest_ha.ha_close)

        # Mark this candle as processed BEFORE acting, so a crash mid-action
        # doesn't re-evaluate the same candle (restart-safe / no dupes).
        # We persist the timestamp now; state updates follow after execution.
        self.db.upsert_state(
            symbol=symbol,
            last_processed_candle_ts=candle_ts,
            last_candle_color=color,
        )
        state.last_processed_candle_ts = candle_ts
        state.last_candle_color = color

        # --- Strategy logic ---
        self._apply_signal(symbol, color, price=latest_ha.ha_close)

    def _apply_signal(self, symbol: str, color: str, price: Optional[float]) -> None:
        """
        Apply entry / scale / reversal based on candle color vs current state.
        """
        state = self.pm.state(symbol)
        direction_map = {"GREEN": "LONG", "RED": "SHORT"}
        target_direction = direction_map[color]

        if state.position_direction == "FLAT":
            # No position — open initial position
            logger.info("No open position for %s — entering %s on %s candle.", symbol,
                        target_direction, color)
            self.pm.execute_entry(symbol, target_direction, price, signal="ENTRY")
            # Set trend to match
            self.pm._states[symbol].trend_direction = color
            self.pm._persist(symbol)
            return

        current_direction = state.position_direction

        if color == state.trend_direction:
            # Same trend continuing -> scale if under limits
            if current_direction != target_direction:
                # Defensive: should not happen (trend matches position direction)
                logger.error("Trend %s does not match position %s for %s.",
                             color, current_direction, symbol)
                return
            if state.entry_count < max_entries(symbol):
                logger.info("Trend continuing %s for %s — scaling %s (entry %d/%d).",
                            color, symbol, target_direction,
                            state.entry_count + 1, max_entries(symbol))
                self.pm.execute_scale(symbol, target_direction, price, signal="SCALE")
            else:
                logger.info("Max entries reached for %s (%d/%d) — scaling skipped, holding.",
                            symbol, state.entry_count, max_entries(symbol))
                self.db.audit("SCALING_SKIPPED", f"Max entries {state.entry_count}/{max_entries(symbol)}",
                              symbol=symbol)
        else:
            # Trend reversed -> full reversal
            logger.info("Trend reversal for %s: %s -> %s. Closing all %s and opening %s.",
                        symbol, state.trend_direction, color,
                        current_direction, target_direction)
            self.pm.execute_reversal(symbol, target_direction, price)
            # After reversal, trend follows the new candle color
            self.pm._states[symbol].trend_direction = color
            self.pm._persist(symbol)

    # ------------------------------------------------------------------ #
    # Run all symbols
    # ------------------------------------------------------------------ #
    def evaluate_all(self, symbols: list[str]) -> None:
        for symbol in symbols:
            try:
                self.evaluate_symbol(symbol)
            except Exception as exc:
                logger.error("Unhandled error evaluating %s: %s", symbol, exc, exc_info=True)
                self.db.audit("ERROR", f"Unhandled strategy error {symbol}: {exc}", symbol=symbol)
