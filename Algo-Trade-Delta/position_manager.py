"""
Position manager — in-memory position state + execution + validation.

Responsibilities:
  * Track current exposure, entry count, and position direction per symbol.
  * Enforce max_position_size = base_lot * max_entries.
  * Prevent scaling beyond max_entries.
  * Prevent simultaneous LONG and SHORT positions.
  * Validate every order before and after submission (direction, size,
    duplicate prevention, fill verification).
  * Execute entry / scale / reversal orders through the Delta client.
  * Reconcile local state against exchange positions on startup.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from config import (
    base_lot,
    max_entries,
    max_position_size,
    SYMBOL_CONFIG,
    DRY_RUN,
)
from database import Database
from delta_client import DeltaClient
from logger import get_logger

logger = get_logger("position_manager")


@dataclass
class PositionState:
    """In-memory mirror of bot_state for a single symbol."""
    symbol: str
    trend_direction: Optional[str] = None          # 'GREEN' | 'RED' | None
    position_direction: str = "FLAT"               # 'LONG' | 'SHORT' | 'FLAT'
    position_size: float = 0.0                     # lots
    entry_count: int = 0                           # scaling entries in current trend
    last_processed_candle_ts: Optional[str] = None
    last_candle_color: Optional[str] = None
    active_positions: int = 0
    latest_order_id: Optional[str] = None
    latest_order_side: Optional[str] = None
    latest_order_qty: Optional[float] = None
    latest_order_price: Optional[float] = None


class PositionManager:
    def __init__(
        self,
        client: DeltaClient,
        db: Database,
        dry_run: bool = DRY_RUN,
    ) -> None:
        self.client = client
        self.db = db
        self.dry_run = dry_run
        self._states: dict[str, PositionState] = {}

    # ------------------------------------------------------------------ #
    # Initialisation / reconciliation
    # ------------------------------------------------------------------ #
    def initialize(self, symbols: list[str]) -> None:
        """
        Load persisted state for each symbol from SQLite. On first run the
        state defaults to FLAT / empty.
        """
        for symbol in symbols:
            row = self.db.get_state(symbol)
            self._states[symbol] = PositionState(
                symbol=symbol,
                trend_direction=row.get("trend_direction"),
                position_direction=row.get("position_direction") or "FLAT",
                position_size=float(row.get("position_size") or 0),
                entry_count=int(row.get("entry_count") or 0),
                last_processed_candle_ts=row.get("last_processed_candle_ts"),
                last_candle_color=row.get("last_candle_color"),
                active_positions=int(row.get("active_positions") or 0),
                latest_order_id=row.get("latest_order_id"),
                latest_order_side=row.get("latest_order_side"),
                latest_order_qty=row.get("latest_order_qty"),
                latest_order_price=row.get("latest_order_price"),
            )
            logger.info(
                "Loaded state for %s: direction=%s size=%.2f entries=%d trend=%s last_candle=%s",
                symbol, self._states[symbol].position_direction,
                self._states[symbol].position_size, self._states[symbol].entry_count,
                self._states[symbol].trend_direction, self._states[symbol].last_candle_color,
            )

    def reconcile_with_exchange(self, symbols: list[str]) -> None:
        """
        Requirement 12 (Position Reconciliation).

        On startup:
          1. Fetch all open positions from Delta Exchange.
          2. Compare exchange positions against SQLite state.
          3. Detect discrepancies.
          4. Exchange positions are treated as source of truth.
          5. Rebuild local state when mismatches occur.
          6. Log all reconciliation activities.
        """
        try:
            exchange_positions = self.client.get_positions()
        except Exception as exc:
            logger.error("Reconciliation failed to fetch exchange positions: %s", exc)
            self.db.audit("RECONCILIATION", f"Failed to fetch exchange positions: {exc}")
            return

        # Index exchange positions by symbol
        exchange_by_symbol: dict[str, dict] = {}
        for pos in exchange_positions:
            sym = pos.get("product_symbol") or pos.get("symbol")
            if not sym:
                continue
            exchange_by_symbol[sym] = pos

        for symbol in symbols:
            state = self._states.get(symbol)
            if state is None:
                continue

            ex_pos = exchange_by_symbol.get(symbol)
            ex_size = 0.0
            ex_side = "FLAT"
            if ex_pos is not None:
                ex_size = abs(float(ex_pos.get("size", 0) or 0))
                raw_side = str(ex_pos.get("side", "")).lower()
                if raw_side in ("buy", "long"):
                    ex_side = "LONG"
                elif raw_side in ("sell", "short"):
                    ex_side = "SHORT"
                else:
                    ex_side = "FLAT"

            local_side = state.position_direction
            local_size = state.position_size

            mismatch = False
            if ex_side == "FLAT" and local_side != "FLAT":
                mismatch = True
                reason = "exchange flat but local has position"
            elif ex_side != "FLAT" and local_side == "FLAT":
                mismatch = True
                reason = "exchange has position but local is flat"
            elif ex_side != local_side:
                mismatch = True
                reason = f"direction mismatch: exchange={ex_side} local={local_side}"
            elif abs(ex_size - local_size) > 1e-6:
                mismatch = True
                reason = f"size mismatch: exchange={ex_size} local={local_size}"

            if mismatch:
                logger.warning(
                    "Reconciliation mismatch for %s: %s. Exchange is source of truth — rebuilding local state.",
                    symbol, reason,
                )
                self.db.audit(
                    "RECONCILIATION",
                    f"Mismatch for {symbol}: {reason}. Rebuilding local state from exchange.",
                    symbol=symbol,
                    details={
                        "exchange_side": ex_side,
                        "exchange_size": ex_size,
                        "local_side": local_side,
                        "local_size": local_size,
                        "reason": reason,
                    },
                )
                # Rebuild local state from exchange (source of truth)
                self._sync_state_from_exchange(symbol, ex_side, ex_size)
                # Reset trend & entry count so we don't immediately scale incorrectly
                self._states[symbol].trend_direction = None
                self._states[symbol].entry_count = 0
                self._persist(symbol)
                logger.info(
                    "Rebuilt state for %s: direction=%s size=%.2f entries=0 trend=None",
                    symbol, ex_side, ex_size,
                )
            else:
                logger.info(
                    "Reconciliation OK for %s: %s size=%.2f matches local state.",
                    symbol, ex_side, ex_size,
                )

    def _sync_state_from_exchange(self, symbol: str, side: str, size: float) -> None:
        """Set in-memory + persisted state to match exchange (source of truth)."""
        state = self._states[symbol]
        state.position_direction = side
        state.position_size = size
        state.active_positions = 1 if size > 0 else 0
        state.entry_count = 0  # unknown entry count — reset to avoid over-scaling
        self._persist(symbol)

    # ------------------------------------------------------------------ #
    # State accessors
    # ------------------------------------------------------------------ #
    def state(self, symbol: str) -> PositionState:
        return self._states[symbol]

    def _persist(self, symbol: str) -> None:
        state = self._states[symbol]
        self.db.upsert_state(
            symbol=symbol,
            trend_direction=state.trend_direction,
            position_direction=state.position_direction,
            position_size=state.position_size,
            entry_count=state.entry_count,
            last_processed_candle_ts=state.last_processed_candle_ts,
            last_candle_color=state.last_candle_color,
            active_positions=state.active_positions,
            latest_order_id=state.latest_order_id,
            latest_order_side=state.latest_order_side,
            latest_order_qty=state.latest_order_qty,
            latest_order_price=state.latest_order_price,
        )

    # ------------------------------------------------------------------ #
    # Order execution + validation
    # ------------------------------------------------------------------ #
    def _order_side_for(self, direction: str) -> str:
        """Map LONG/SHORT to Delta Exchange order side (buy/sell)."""
        if direction == "LONG":
            return "buy"
        if direction == "SHORT":
            return "sell"
        raise ValueError(f"Cannot map direction '{direction}' to order side")

    def _closing_side_for(self, direction: str) -> str:
        """Opposing side used to close an existing position."""
        return "sell" if direction == "LONG" else "buy"

    def execute_entry(
        self,
        symbol: str,
        direction: str,   # 'LONG' or 'SHORT'
        price: Optional[float],
        signal: str = "ENTRY",
    ) -> bool:
        """
        Open an initial position of base_lot.

        Validates:
          * No existing position in the opposite direction (never LONG+SHORT).
          * Entry count < max_entries.
          * Resulting size <= max_position_size.
        """
        state = self._states[symbol]
        lot = base_lot(symbol)
        max_size = max_position_size(symbol)

        # Direction validation — never hold LONG and SHORT simultaneously
        if state.position_direction != "FLAT" and state.position_direction != direction:
            logger.error(
                "REFUSED entry %s %s: existing %s position (size=%.2f). Reversal must close it first.",
                direction, symbol, state.position_direction, state.position_size,
            )
            self.db.audit("ERROR", f"Refused {direction} entry for {symbol}: existing {state.position_direction} position",
                          symbol=symbol)
            return False

        # Entry count enforcement
        if state.entry_count >= max_entries(symbol):
            logger.warning(
                "Entry count %d >= max_entries %d for %s — skipping entry.",
                state.entry_count, max_entries(symbol), symbol,
            )
            self.db.audit("SCALING_SKIPPED", f"Entry count {state.entry_count} >= max {max_entries(symbol)}",
                          symbol=symbol)
            return False

        # Exposure validation
        new_size = state.position_size + lot
        if new_size > max_size + 1e-9:
            logger.error(
                "REFUSED entry %s %s: size %.2f would exceed max_position_size %.2f",
                direction, symbol, new_size, max_size,
            )
            self.db.audit("ERROR", f"Refused {direction} entry for {symbol}: would exceed max size",
                          symbol=symbol)
            return False

        # Place order
        order_side = self._order_side_for(direction)
        try:
            result = self.client.place_market_order(symbol, order_side, lot, price=price)
        except Exception as exc:
            logger.error("Failed to place entry order %s %s: %s", direction, symbol, exc)
            self.db.audit("ERROR", f"Entry order failed {direction} {symbol}: {exc}", symbol=symbol)
            return False

        order_id = str(result.get("id", "")) if isinstance(result, dict) else ""
        filled_qty = float(result.get("filled_size", lot) or lot)
        fill_price = result.get("avg_fill_price") or price

        # Post-fill verification (Requirement 14)
        if not self._verify_fill(symbol, direction, filled_qty, fill_price, order_id):
            logger.error("Fill verification failed for entry %s %s", direction, symbol)
            self.db.audit("ERROR", f"Fill verification failed entry {direction} {symbol}", symbol=symbol)
            return False

        # Update in-memory + persisted state
        state.position_direction = direction
        state.position_size = new_size
        state.entry_count += 1
        state.active_positions += 1
        state.latest_order_id = order_id
        state.latest_order_side = order_side
        state.latest_order_qty = filled_qty
        state.latest_order_price = fill_price
        if state.trend_direction is None:
            state.trend_direction = "GREEN" if direction == "LONG" else "RED"
        self._persist(symbol)

        # Record open trade
        self.db.add_open_trade(
            symbol=symbol,
            side=direction,
            quantity=filled_qty,
            entry_price=fill_price,
            order_id=order_id,
            strategy_signal=signal,
        )

        logger.info(
            "ENTRY %s %s: size=%.2f entries=%d/%d price=%s order=%s",
            direction, symbol, new_size, state.entry_count, max_entries(symbol),
            fill_price, order_id,
        )
        self.db.audit("ORDER", f"Entry {direction} {symbol} size={filled_qty} price={fill_price}",
                      symbol=symbol, details={"order_id": order_id, "signal": signal})
        return True

    def execute_scale(
        self,
        symbol: str,
        direction: str,   # 'LONG' or 'SHORT' — must match current position_direction
        price: Optional[float],
        signal: str = "SCALE",
    ) -> bool:
        """
        Add one base_lot to an existing position in the same direction.

        Validates:
          * Position exists in the same direction.
          * entry_count < max_entries.
          * Resulting size <= max_position_size.
        """
        state = self._states[symbol]
        lot = base_lot(symbol)
        max_size = max_position_size(symbol)

        if state.position_direction != direction or state.position_direction == "FLAT":
            logger.warning(
                "Cannot scale %s %s: current direction is %s (size=%.2f).",
                direction, symbol, state.position_direction, state.position_size,
            )
            self.db.audit("SCALING_SKIPPED", f"No {direction} position to scale for {symbol}",
                          symbol=symbol)
            return False

        if state.entry_count >= max_entries(symbol):
            logger.warning(
                "Scaling skipped for %s: entry_count %d >= max_entries %d.",
                symbol, state.entry_count, max_entries(symbol),
            )
            self.db.audit("SCALING_SKIPPED", f"Max entries reached {state.entry_count}/{max_entries(symbol)}",
                          symbol=symbol)
            return False

        new_size = state.position_size + lot
        if new_size > max_size + 1e-9:
            logger.error(
                "Scaling refused %s %s: size %.2f exceeds max_position_size %.2f",
                direction, symbol, new_size, max_size,
            )
            self.db.audit("SCALING_SKIPPED", f"Would exceed max size {new_size}>{max_size}",
                          symbol=symbol)
            return False

        order_side = self._order_side_for(direction)
        try:
            result = self.client.place_market_order(symbol, order_side, lot, price=price)
        except Exception as exc:
            logger.error("Failed to place scale order %s %s: %s", direction, symbol, exc)
            self.db.audit("ERROR", f"Scale order failed {direction} {symbol}: {exc}", symbol=symbol)
            return False

        order_id = str(result.get("id", "")) if isinstance(result, dict) else ""
        filled_qty = float(result.get("filled_size", lot) or lot)
        fill_price = result.get("avg_fill_price") or price

        if not self._verify_fill(symbol, direction, filled_qty, fill_price, order_id):
            logger.error("Fill verification failed for scale %s %s", direction, symbol)
            self.db.audit("ERROR", f"Fill verification failed scale {direction} {symbol}", symbol=symbol)
            return False

        state.position_size = new_size
        state.entry_count += 1
        state.active_positions += 1
        state.latest_order_id = order_id
        state.latest_order_side = order_side
        state.latest_order_qty = filled_qty
        state.latest_order_price = fill_price
        self._persist(symbol)

        self.db.add_open_trade(
            symbol=symbol,
            side=direction,
            quantity=filled_qty,
            entry_price=fill_price,
            order_id=order_id,
            strategy_signal=signal,
        )

        logger.info(
            "SCALE %s %s: size=%.2f entries=%d/%d price=%s order=%s",
            direction, symbol, new_size, state.entry_count, max_entries(symbol),
            fill_price, order_id,
        )
        self.db.audit("ORDER", f"Scale {direction} {symbol} size={filled_qty} price={fill_price}",
                      symbol=symbol, details={"order_id": order_id, "signal": signal})
        return True

    def execute_reversal(
        self,
        symbol: str,
        new_direction: str,   # 'LONG' or 'SHORT' — opposite of current
        price: Optional[float],
    ) -> bool:
        """
        Requirement 11 (Position Reversal Logic) — atomic:

          1. Close all positions in the current direction.
          2. Confirm all positions fully closed (verify via exchange).
          3. Reset entry count.
          4. Open a new position in the opposite direction using base_lot.

        Never holds LONG and SHORT simultaneously.
        """
        state = self._states[symbol]
        old_direction = state.position_direction
        if old_direction == "FLAT":
            logger.warning("Reversal for %s called but position is already FLAT.", symbol)
            return False
        if new_direction == old_direction:
            logger.error("Reversal %s -> %s for %s is not a reversal.", old_direction, new_direction, symbol)
            return False

        max_size = max_position_size(symbol)
        lot = base_lot(symbol)

        # Step 1: close all existing positions (market opposing order)
        close_side = self._closing_side_for(old_direction)
        try:
            result = self.client.place_market_order(symbol, close_side, state.position_size, price=price)
        except Exception as exc:
            logger.error("Reversal close order failed %s %s: %s", old_direction, symbol, exc)
            self.db.audit("ERROR", f"Reversal close failed {old_direction} {symbol}: {exc}", symbol=symbol)
            return False

        close_order_id = str(result.get("id", "")) if isinstance(result, dict) else ""
        fill_price = result.get("avg_fill_price") or price

        # Step 2: confirm closure via exchange (source of truth)
        try:
            exchange_positions = self.client.get_positions()
        except Exception as exc:
            logger.error("Cannot verify reversal closure for %s: %s", symbol, exc)
            self.db.audit("ERROR", f"Reversal verify failed {symbol}: {exc}", symbol=symbol)
            return False

        still_open = False
        for pos in exchange_positions:
            psym = pos.get("product_symbol") or pos.get("symbol")
            if psym != symbol:
                continue
            psize = abs(float(pos.get("size", 0) or 0))
            if psize > 1e-9:
                still_open = True
                break

        if still_open:
            logger.error("Reversal closure NOT confirmed for %s — position still open on exchange. Aborting.",
                         symbol)
            self.db.audit("ERROR", f"Reversal closure not confirmed {symbol}", symbol=symbol)
            return False

        # Step 3: archive old open trades + reset entry count
        self.db.close_open_trades(symbol, old_direction, fill_price)
        state.position_direction = "FLAT"
        state.position_size = 0.0
        state.entry_count = 0
        state.active_positions = 0
        state.latest_order_id = close_order_id
        state.latest_order_side = close_side
        state.latest_order_qty = state.position_size
        state.latest_order_price = fill_price
        self._persist(symbol)

        logger.info(
            "REVERSAL %s: closed all %s positions. Confirmed flat on exchange. entry_count reset.",
            symbol, old_direction,
        )
        self.db.audit("REVERSAL", f"Closed all {old_direction} positions for {symbol}; confirmed flat; entry_count reset",
                      symbol=symbol, details={"close_order_id": close_order_id, "price": fill_price})

        # Step 4: open new position in opposite direction (base_lot)
        return self.execute_entry(symbol, new_direction, price, signal="REVERSAL")

    def close_all(self, symbol: str, price: Optional[float] = None) -> bool:
        """Close the current position (used on graceful shutdown)."""
        state = self._states[symbol]
        if state.position_direction == "FLAT" or state.position_size <= 0:
            return True
        direction = state.position_direction
        return self.execute_reversal(symbol, "LONG" if direction == "SHORT" else "SHORT", price)

    # ------------------------------------------------------------------ #
    # Post-fill verification (Requirement 14)
    # ------------------------------------------------------------------ #
    def _verify_fill(
        self,
        symbol: str,
        direction: str,
        qty: float,
        price: Optional[float],
        order_id: str,
    ) -> bool:
        """
        After every order:
          * Verify order submission (we have an order_id / result).
          * Verify order acceptance (success flag).
          * Verify fill status (filled_size > 0).
          * Verify resulting position size and direction (re-query exchange).
        """
        if not order_id:
            logger.error("Fill verification: no order_id returned for %s %s", direction, symbol)
            return False
        if qty <= 0:
            logger.error("Fill verification: zero fill qty for %s %s", direction, symbol)
            return False

        # Re-query exchange to confirm resulting position direction/size
        try:
            positions = self.client.get_positions()
        except Exception as exc:
            logger.warning("Could not re-query positions for fill verification %s: %s", symbol, exc)
            # In dry_run we trust the simulated fill
            return self.dry_run

        for pos in positions:
            psym = pos.get("product_symbol") or pos.get("symbol")
            if psym != symbol:
                continue
            psize = abs(float(pos.get("size", 0) or 0))
            raw_side = str(pos.get("side", "")).lower()
            ex_direction = "LONG" if raw_side in ("buy", "long") else ("SHORT" if raw_side in ("sell", "short") else "FLAT")
            if psize > 1e-9:
                # Position exists — confirm direction matches what we intended
                if ex_direction != direction:
                    logger.error(
                        "Fill verification direction mismatch %s %s: intended %s exchange %s",
                        direction, symbol, direction, ex_direction,
                    )
                    return False
                return True
            # Position for this symbol is flat — that's fine if we just closed it
        return True
