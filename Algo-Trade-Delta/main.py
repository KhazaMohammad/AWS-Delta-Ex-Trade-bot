"""
Main entry point for the Algo-Trade-Delta trading system.

Runs continuously:
  * Validates config, initialises DB + Delta client + position manager.
  * Reconciles local state against the exchange (source of truth).
  * Loop: wake every CHECK_INTERVAL_MINUTES, check whether a new 2-hour
    candle has closed, evaluate the strategy for each symbol, sleep.
  * Graceful shutdown on SIGINT / SIGTERM: finish current operation, save
    state, commit SQLite, close connections, write shutdown log, exit cleanly.

Paper trading (DRY_RUN=True) simulates orders without touching the exchange.
Set DRY_RUN=False in .env for live trading.
"""

from __future__ import annotations

import signal
import sys
import time
from typing import Optional

from config import (
    CHECK_INTERVAL_MINUTES,
    DB_PATH,
    DRY_RUN,
    DELTA_API_KEY,
    DELTA_API_SECRET,
    DELTA_BASE_URL,
    SYMBOL_CONFIG,
    validate_config,
)
from database import Database
from delta_client import DeltaClient, DeltaAuthError, DeltaClientError
from logger import get_logger
from position_manager import PositionManager
from strategy import Strategy

logger = get_logger("main")


class TradingBot:
    def __init__(self) -> None:
        self.db: Optional[Database] = None
        self.client: Optional[DeltaClient] = None
        self.pm: Optional[PositionManager] = None
        self.strategy: Optional[Strategy] = None
        self._running = True
        self.symbols: list[str] = []

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def setup(self) -> None:
        logger.info("=" * 72)
        logger.info("Algo-Trade-Delta starting up")
        logger.info("DRY_RUN=%s | symbols=%s | check_interval=%d min",
                    DRY_RUN, list(SYMBOL_CONFIG.keys()), CHECK_INTERVAL_MINUTES)
        logger.info("=" * 72)

        # 1. Validate config (creds, symbol config, etc.)
        validate_config()
        logger.info("Configuration validated")

        # 2. Database
        self.db = Database(DB_PATH)
        self.db.initialize()
        logger.info("Database initialised at %s", DB_PATH)

        # 3. Delta Exchange client
        self.client = DeltaClient(
            api_key=DELTA_API_KEY,
            api_secret=DELTA_API_SECRET,
            base_url=DELTA_BASE_URL,
            dry_run=DRY_RUN,
        )
        logger.info("Delta Exchange client initialised (dry_run=%s)", DRY_RUN)

        # 4. Position manager + strategy
        self.pm = PositionManager(client=self.client, db=self.db, dry_run=DRY_RUN)
        self.strategy = Strategy(client=self.client, db=self.db, position_manager=self.pm)
        self.symbols = list(SYMBOL_CONFIG.keys())

        # 5. Load persisted state
        self.pm.initialize(self.symbols)

        # 6. Reconcile with exchange (source of truth) — restart-safe
        self.pm.reconcile_with_exchange(self.symbols)

        logger.info("Startup complete. Entering main loop.")

    # ------------------------------------------------------------------ #
    # Main loop
    # ------------------------------------------------------------------ #
    def run(self) -> None:
        if self.strategy is None or self.pm is None:
            raise RuntimeError("Bot not set up. Call setup() first.")

        # Install signal handlers for graceful shutdown
        signal.signal(signal.SIGINT, self._handle_signal)
        signal.signal(signal.SIGTERM, self._handle_signal)

        # Run an immediate evaluation on startup (in case a candle closed
        # while the bot was down), then enter the sleep/wake loop.
        self._evaluate_cycle()

        while self._running:
            try:
                time.sleep(CHECK_INTERVAL_MINUTES * 60)
            except InterruptedError:
                pass
            if not self._running:
                break
            self._evaluate_cycle()

    def _evaluate_cycle(self) -> None:
        """One wake-up cycle: evaluate strategy for all symbols."""
        logger.info("Wake-up cycle: evaluating %d symbol(s)", len(self.symbols))
        if self.strategy is None:
            return
        self.strategy.evaluate_all(self.symbols)

        # Refresh performance metrics for each symbol
        self._write_metrics()

        logger.info("Cycle complete. Sleeping %d minutes.", CHECK_INTERVAL_MINUTES)

    def _write_metrics(self) -> None:
        """Write current performance metrics to SQLite for each symbol."""
        if self.db is None or self.pm is None:
            return
        for symbol in self.symbols:
            state = self.pm.state(symbol)
            trades = self.db.get_trade_history(symbol)
            total = len(trades)
            wins = sum(1 for t in trades if (t.get("pnl") or 0) > 0)
            losses = sum(1 for t in trades if (t.get("pnl") or 0) < 0)
            net_pnl = sum((t.get("pnl") or 0) for t in trades)
            avg_pnl = (net_pnl / total) if total else 0.0
            win_rate = (wins / total * 100.0) if total else 0.0
            self.db.upsert_performance_metrics(
                symbol=symbol,
                total_trades=total,
                winning_trades=wins,
                losing_trades=losses,
                win_rate=win_rate,
                net_pnl=net_pnl,
                avg_pnl=avg_pnl,
                current_exposure=state.position_size,
                open_positions=state.active_positions,
            )

    # ------------------------------------------------------------------ #
    # Graceful shutdown
    # ------------------------------------------------------------------ #
    def _handle_signal(self, signum, frame) -> None:
        name = "SIGINT" if signum == signal.SIGINT else "SIGTERM"
        logger.info("Received %s — beginning graceful shutdown.", name)
        self._running = False

    def shutdown(self) -> None:
        logger.info("=" * 72)
        logger.info("Algo-Trade-Delta shutting down")
        logger.info("Finishing current operation and saving state...")
        # State is persisted after every action, so nothing extra to flush,
        # but ensure metrics are written and DB connection closed.
        try:
            self._write_metrics()
        except Exception as exc:
            logger.warning("Could not write final metrics: %s", exc)
        if self.db is not None:
            self.db.close()
        logger.info("Algo-Trade-Delta stopped cleanly.")
        logger.info("=" * 72)


def main() -> None:
    bot = TradingBot()
    try:
        bot.setup()
        bot.run()
    except DeltaAuthError as exc:
        logger.error("Authentication error: %s", exc)
        sys.exit(2)
    except DeltaClientError as exc:
        logger.error("Delta client error: %s", exc)
        sys.exit(3)
    except KeyboardInterrupt:
        logger.info("Interrupted by user (KeyboardInterrupt).")
    finally:
        bot.shutdown()


if __name__ == "__main__":
    main()
