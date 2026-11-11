"""
Configuration for the Algo-Trade-Delta trading system.

All hardcoded trading parameters live here. Exchange credentials are NOT
stored here — they come from the .env file via environment variables
(see .env.example). Symbol lot sizes and limits are configured here so
new symbols can be added by configuration only.
"""

from __future__ import annotations

import os
from dotenv import load_dotenv

# ---------------------------------------------------------------------------
# Load environment variables from .env (if present)
# ---------------------------------------------------------------------------
load_dotenv()

# ---------------------------------------------------------------------------
# Delta Exchange API credentials (from environment — never hardcoded)
# ---------------------------------------------------------------------------
DELTA_API_KEY: str | None = os.getenv("DELTA_API_KEY")
DELTA_API_SECRET: str | None = os.getenv("DELTA_API_SECRET")

# Base URL for Delta Exchange India REST API
DELTA_BASE_URL: str = os.getenv("DELTA_BASE_URL", "https://api.india.delta.exchange")

# ---------------------------------------------------------------------------
# Database & logging paths
# ---------------------------------------------------------------------------
DB_PATH: str = os.getenv("DB_PATH", "trading.db")
LOG_LEVEL: str = os.getenv("LOG_LEVEL", "INFO").upper()

# ---------------------------------------------------------------------------
# Execution loop
# ---------------------------------------------------------------------------
CHECK_INTERVAL_MINUTES: int = int(os.getenv("CHECK_INTERVAL_MINUTES", "60"))

# Paper / dry-run mode (configurable via env var)
DRY_RUN: bool = os.getenv("DRY_RUN", "True").strip().lower() in ("true", "1", "yes")

# ---------------------------------------------------------------------------
# Symbol configuration
#
# Do NOT hardcode lot sizes or position limits elsewhere in the codebase.
# max_position_size is derived as base_lot * max_entries.
# Add future symbols here by configuration only.
# ---------------------------------------------------------------------------
SYMBOL_CONFIG: dict[str, dict[str, int]] = {
    "ETHUSD": {
        "base_lot": 5,
        "max_entries": 5,
    },
    "OILUSD": {
        "base_lot": 2,
        "max_entries": 5,
    },
}

# ---------------------------------------------------------------------------
# Strategy
# ---------------------------------------------------------------------------
# Timeframe for candles (in minutes). 2 hours = 120 minutes.
CANDLE_TIMEFRAME_MINUTES: int = 120
# How many historical candles to fetch when initializing / backfilling.
HISTORICAL_CANDLE_LIMIT: int = 200
# Use Heiken Ashi candles derived from OHLC data.
USE_HEIKEN_ASHI: bool = True
# Only act on fully closed candles.
USE_CLOSED_CANDLES_ONLY: bool = True

# ---------------------------------------------------------------------------
# Derived helpers
# ---------------------------------------------------------------------------

def max_position_size(symbol: str) -> int:
    """Return max_position_size = base_lot * max_entries for a symbol."""
    cfg = SYMBOL_CONFIG[symbol]
    return cfg["base_lot"] * cfg["max_entries"]

def base_lot(symbol: str) -> int:
    return SYMBOL_CONFIG[symbol]["base_lot"]

def max_entries(symbol: str) -> int:
    return SYMBOL_CONFIG[symbol]["max_entries"]

def validate_config() -> None:
    """Validate required configuration at startup. Raises on missing creds."""
    missing = []
    if not DELTA_API_KEY:
        missing.append("DELTA_API_KEY")
    if not DELTA_API_SECRET:
        missing.append("DELTA_API_SECRET")
    if missing:
        raise RuntimeError(
            f"Missing required environment variables: {', '.join(missing)}. "
            "Copy .env.example to .env and fill in your Delta Exchange API credentials."
        )
    if not SYMBOL_CONFIG:
        raise RuntimeError("SYMBOL_CONFIG is empty — at least one symbol must be configured.")
    for symbol, cfg in SYMBOL_CONFIG.items():
        if cfg["base_lot"] <= 0 or cfg["max_entries"] <= 0:
            raise RuntimeError(f"Invalid config for {symbol}: {cfg}")
