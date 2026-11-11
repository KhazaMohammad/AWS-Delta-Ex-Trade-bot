"""
SQLite persistence layer for the Algo-Trade-Delta trading system.

Persists (per symbol):
  - trend direction (current Heiken Ashi candle color)
  - position direction (LONG / SHORT / FLAT)
  - position size (lots)
  - entry count (number of scaling entries in current trend)
  - last processed candle timestamp
  - last candle color
  - active positions
  - open trades
  - latest order information
  - trade history (completed trades with PnL)

The bot recovers and continues correctly after restart without generating
duplicate trades: every candle is processed exactly once (guarded by
last_processed_candle_ts) and every order is recorded before/after submission.
"""

from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timezone
from typing import Any, Optional

from config import LOG_LEVEL, SYMBOL_CONFIG


# ---------------------------------------------------------------------------
# Connection management (thread-safe, check_same_thread=False so the single
# background thread can use it across calls; a lock serializes access)
# ---------------------------------------------------------------------------

class Database:
    def __init__(self, db_path: str = "trading.db") -> None:
        self.db_path = db_path
        self._lock = threading.Lock()
        # check_same_thread=False allows usage from any thread; the lock
        # guarantees serialised access so SQLite does not complain.
        self._conn: Optional[sqlite3.Connection] = None
        self.initialize()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, check_same_thread=False, timeout=30.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        return conn

    @property
    def conn(self) -> sqlite3.Connection:
        if self._conn is None:
            self._conn = self._connect()
        return self._conn

    @contextmanager
    def transaction(self):
        """Provide a transactional scope around a series of operations."""
        with self._lock:
            conn = self.conn
            try:
                yield conn
                conn.commit()
            except Exception:
                conn.rollback()
                raise

    def close(self) -> None:
        with self._lock:
            if self._conn is not None:
                self._conn.close()
                self._conn = None

    # ------------------------------------------------------------------
    # Schema
    # ------------------------------------------------------------------
    def initialize(self) -> None:
        """Create all tables if they do not already exist."""
        with self.transaction() as conn:
            # Bot state per symbol
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS bot_state (
                    symbol            TEXT PRIMARY KEY,
                    trend_direction   TEXT,            -- 'GREEN' | 'RED' | NULL
                    position_direction TEXT,           -- 'LONG' | 'SHORT' | 'FLAT'
                    position_size     REAL DEFAULT 0,  -- lots
                    entry_count       INTEGER DEFAULT 0,
                    last_processed_candle_ts TEXT,     -- ISO8601 UTC
                    last_candle_color TEXT,            -- 'GREEN' | 'RED' | NULL
                    active_positions  INTEGER DEFAULT 0,
                    latest_order_id   TEXT,
                    latest_order_side TEXT,
                    latest_order_qty  REAL,
                    latest_order_price REAL,
                    updated_at        TEXT
                )
                """
            )

            # Open trades (not yet closed / reversed)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS open_trades (
                    id             INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol         TEXT NOT NULL,
                    side           TEXT NOT NULL,      -- 'LONG' | 'SHORT'
                    quantity       REAL NOT NULL,      -- lots
                    entry_price    REAL,
                    entry_time     TEXT NOT NULL,      -- ISO8601 UTC
                    order_id       TEXT,
                    strategy_signal TEXT,              -- 'ENTRY' | 'SCALE' | 'REVERSAL'
                    status         TEXT DEFAULT 'open' -- 'open' | 'closed' | 'reversed'
                )
                """
            )

            # Completed trade history (for auditing & analytics)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS trade_history (
                    trade_id         INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol           TEXT NOT NULL,
                    side             TEXT NOT NULL,
                    quantity         REAL NOT NULL,
                    entry_price      REAL,
                    exit_price       REAL,
                    pnl              REAL,
                    entry_time       TEXT NOT NULL,
                    exit_time        TEXT,
                    strategy_signal  TEXT,
                    order_id         TEXT
                )
                """
            )

            # Performance metrics snapshot (periodically written)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS performance_metrics (
                    id               INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol           TEXT NOT NULL,
                    total_trades     INTEGER DEFAULT 0,
                    winning_trades   INTEGER DEFAULT 0,
                    losing_trades    INTEGER DEFAULT 0,
                    win_rate         REAL DEFAULT 0,
                    net_pnl          REAL DEFAULT 0,
                    avg_pnl          REAL DEFAULT 0,
                    current_exposure REAL DEFAULT 0,
                    open_positions   INTEGER DEFAULT 0,
                    calculated_at    TEXT NOT NULL
                )
                """
            )

            # Audit log of every significant event (structured)
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS audit_log (
                    id          INTEGER PRIMARY KEY AUTOINCREMENT,
                    symbol      TEXT,
                    event_type  TEXT NOT NULL,   -- 'SIGNAL' | 'ORDER' | 'FILL' | 'REVERSAL' | 'SCALING_SKIPPED' | 'ERROR' | 'RECONCILIATION'
                    message     TEXT NOT NULL,
                    details     TEXT,            -- JSON payload
                    created_at  TEXT NOT NULL
                )
                """
            )

    # ------------------------------------------------------------------
    # Bot state helpers
    # ------------------------------------------------------------------
    def upsert_state(
        self,
        symbol: str,
        *,
        trend_direction: Optional[str] = None,
        position_direction: Optional[str] = None,
        position_size: Optional[float] = None,
        entry_count: Optional[int] = None,
        last_processed_candle_ts: Optional[str] = None,
        last_candle_color: Optional[str] = None,
        active_positions: Optional[int] = None,
        latest_order_id: Optional[str] = None,
        latest_order_side: Optional[str] = None,
        latest_order_qty: Optional[float] = None,
        latest_order_price: Optional[float] = None,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            # Ensure row exists
            conn.execute(
                "INSERT OR IGNORE INTO bot_state (symbol, updated_at) VALUES (?, ?)",
                (symbol, now),
            )
            sets = []
            params: list[Any] = []
            if trend_direction is not None:
                sets.append("trend_direction = ?"); params.append(trend_direction)
            if position_direction is not None:
                sets.append("position_direction = ?"); params.append(position_direction)
            if position_size is not None:
                sets.append("position_size = ?"); params.append(position_size)
            if entry_count is not None:
                sets.append("entry_count = ?"); params.append(entry_count)
            if last_processed_candle_ts is not None:
                sets.append("last_processed_candle_ts = ?"); params.append(last_processed_candle_ts)
            if last_candle_color is not None:
                sets.append("last_candle_color = ?"); params.append(last_candle_color)
            if active_positions is not None:
                sets.append("active_positions = ?"); params.append(active_positions)
            if latest_order_id is not None:
                sets.append("latest_order_id = ?"); params.append(latest_order_id)
            if latest_order_side is not None:
                sets.append("latest_order_side = ?"); params.append(latest_order_side)
            if latest_order_qty is not None:
                sets.append("latest_order_qty = ?"); params.append(latest_order_qty)
            if latest_order_price is not None:
                sets.append("latest_order_price = ?"); params.append(latest_order_price)
            sets.append("updated_at = ?"); params.append(now)
            params.append(symbol)
            conn.execute(
                f"UPDATE bot_state SET {', '.join(sets)} WHERE symbol = ?",
                params,
            )

    def get_state(self, symbol: str) -> dict[str, Any]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM bot_state WHERE symbol = ?", (symbol,)
            ).fetchone()
        if row is None:
            return {
                "symbol": symbol,
                "trend_direction": None,
                "position_direction": "FLAT",
                "position_size": 0.0,
                "entry_count": 0,
                "last_processed_candle_ts": None,
                "last_candle_color": None,
                "active_positions": 0,
                "latest_order_id": None,
                "latest_order_side": None,
                "latest_order_qty": None,
                "latest_order_price": None,
            }
        return dict(row)

    # ------------------------------------------------------------------
    # Open trades
    # ------------------------------------------------------------------
    def add_open_trade(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: Optional[float],
        order_id: Optional[str],
        strategy_signal: str,
    ) -> int:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO open_trades
                    (symbol, side, quantity, entry_price, entry_time, order_id, strategy_signal, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, 'open')
                """,
                (symbol, side, quantity, entry_price, now, order_id, strategy_signal),
            )
            return cur.lastrowid

    def close_open_trades(self, symbol: str, side: str, exit_price: Optional[float]) -> list[dict[str, Any]]:
        """
        Close all open trades of a given side for a symbol (used on reversal).
        Moves them to trade_history with PnL computed against exit_price.
        """
        now = datetime.now(timezone.utc).isoformat()
        archived: list[dict[str, Any]] = []
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM open_trades WHERE symbol = ? AND side = ? AND status = 'open'",
                (symbol, side),
            ).fetchall()
            for row in rows:
                row = dict(row)
                entry_price = row["entry_price"]
                qty = row["quantity"]
                pnl: Optional[float] = None
                if entry_price is not None and exit_price is not None:
                    # Long: exit - entry ; Short: entry - exit
                    if side == "LONG":
                        pnl = (exit_price - entry_price) * qty
                    else:
                        pnl = (entry_price - exit_price) * qty
                conn.execute(
                    """
                    INSERT INTO trade_history
                        (symbol, side, quantity, entry_price, exit_price, pnl,
                         entry_time, exit_time, strategy_signal, order_id)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        row["symbol"], row["side"], row["quantity"],
                        entry_price, exit_price, pnl,
                        row["entry_time"], now,
                        row["strategy_signal"], row["order_id"],
                    ),
                )
                conn.execute(
                    "UPDATE open_trades SET status = 'closed' WHERE id = ?",
                    (row["id"],),
                )
                archived.append({
                    **row,
                    "exit_price": exit_price,
                    "pnl": pnl,
                    "exit_time": now,
                })
        return archived

    def get_open_trades(self, symbol: str) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM open_trades WHERE symbol = ? AND status = 'open'",
                (symbol,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Trade history
    # ------------------------------------------------------------------
    def add_trade_history(
        self,
        symbol: str,
        side: str,
        quantity: float,
        entry_price: Optional[float],
        exit_price: Optional[float],
        pnl: Optional[float],
        entry_time: str,
        exit_time: Optional[str],
        strategy_signal: str,
        order_id: Optional[str],
    ) -> int:
        with self.transaction() as conn:
            cur = conn.execute(
                """
                INSERT INTO trade_history
                    (symbol, side, quantity, entry_price, exit_price, pnl,
                     entry_time, exit_time, strategy_signal, order_id)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, side, quantity, entry_price, exit_price, pnl,
                 entry_time, exit_time, strategy_signal, order_id),
            )
            return cur.lastrowid

    def get_trade_history(self, symbol: str) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            rows = conn.execute(
                "SELECT * FROM trade_history WHERE symbol = ? ORDER BY entry_time",
                (symbol,),
            ).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Performance metrics
    # ------------------------------------------------------------------
    def upsert_performance_metrics(
        self,
        symbol: str,
        total_trades: int,
        winning_trades: int,
        losing_trades: int,
        win_rate: float,
        net_pnl: float,
        avg_pnl: float,
        current_exposure: float,
        open_positions: int,
    ) -> None:
        now = datetime.now(timezone.utc).isoformat()
        with self.transaction() as conn:
            conn.execute(
                """
                INSERT INTO performance_metrics
                    (symbol, total_trades, winning_trades, losing_trades, win_rate,
                     net_pnl, avg_pnl, current_exposure, open_positions, calculated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (symbol, total_trades, winning_trades, losing_trades, win_rate,
                 net_pnl, avg_pnl, current_exposure, open_positions, now),
            )

    def get_latest_performance(self, symbol: str) -> Optional[dict[str, Any]]:
        with self.transaction() as conn:
            row = conn.execute(
                "SELECT * FROM performance_metrics WHERE symbol = ? ORDER BY calculated_at DESC LIMIT 1",
                (symbol,),
            ).fetchone()
        return dict(row) if row else None

    # ------------------------------------------------------------------
    # Audit log
    # ------------------------------------------------------------------
    def audit(
        self,
        event_type: str,
        message: str,
        *,
        symbol: Optional[str] = None,
        details: Optional[dict[str, Any]] = None,
    ) -> None:
        import json
        now = datetime.now(timezone.utc).isoformat()
        details_json = json.dumps(details, default=str) if details is not None else None
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO audit_log (symbol, event_type, message, details, created_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (symbol, event_type, message, details_json, now),
            )

    def get_audit_log(self, symbol: Optional[str] = None, limit: int = 100) -> list[dict[str, Any]]:
        with self.transaction() as conn:
            if symbol is not None:
                rows = conn.execute(
                    "SELECT * FROM audit_log WHERE symbol = ? ORDER BY id DESC LIMIT ?",
                    (symbol, limit),
                ).fetchall()
            else:
                rows = conn.execute(
                    "SELECT * FROM audit_log ORDER BY id DESC LIMIT ?", (limit,)
                ).fetchall()
        return [dict(r) for r in rows]
