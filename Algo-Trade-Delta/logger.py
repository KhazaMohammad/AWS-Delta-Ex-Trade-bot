"""
Structured rotating logging for the Algo-Trade-Delta trading system.

Uses logging.handlers.RotatingFileHandler so log files stay bounded and are
rotated automatically. Logs cover: raw candle data, OHLC values, Heiken Ashi
calculations, signals, position changes, scaling actions, reversals, orders
submitted/filled, ignored scaling signals, exchange responses, errors,
warnings, startup and shutdown events.
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import sys
from typing import Optional

from config import LOG_LEVEL


LOG_DIR = "logs"
LOG_FILE = os.path.join(LOG_DIR, "algo_trade_delta.log")
MAX_BYTES = 10 * 1024 * 1024   # 10 MB per log file
BACKUP_COUNT = 5               # keep 5 rotated files

# Structured log format: timestamp | level | module | message
_FMT = "%(asctime)s | %(levelname)-8s | %(name)-20s | %(message)s"
_DATEFMT = "%Y-%m-%d %H:%M:%S"

_configured = False


def get_logger(name: str = "algo_trade_delta") -> logging.Logger:
    """Return a configured logger with rotating file handler + console output."""
    global _configured
    logger = logging.getLogger(name)
    if logger.handlers:
        # Already configured
        return logger

    os.makedirs(LOG_DIR, exist_ok=True)

    level = getattr(logging, LOG_LEVEL, logging.INFO)
    logger.setLevel(level)
    logger.propagate = False

    formatter = logging.Formatter(_FMT, datefmt=_DATEFMT)

    # Rotating file handler
    fh = logging.handlers.RotatingFileHandler(
        LOG_FILE, maxBytes=MAX_BYTES, backupCount=BACKUP_COUNT, encoding="utf-8"
    )
    fh.setLevel(level)
    fh.setFormatter(formatter)
    logger.addHandler(fh)

    # Console handler (stdout) — useful when run under systemd/journald
    sh = logging.StreamHandler(sys.stdout)
    sh.setLevel(level)
    sh.setFormatter(formatter)
    logger.addHandler(sh)

    _configured = True
    return logger
