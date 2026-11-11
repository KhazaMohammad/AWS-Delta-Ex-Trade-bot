# Algo-Trade-Delta

Production-ready Python algorithmic trading system for **Delta Exchange
perpetual futures** (ETHUSD, OILUSD). Built to run 24x7 on an AWS EC2 Free
Tier instance. Fully modular, fault-tolerant, restart-safe, and
deployment-ready.

> ⚠️  Paper trading is enabled by default (`DRY_RUN=True`). Set `DRY_RUN=False`
> in `.env` only when you are ready to trade real funds.

## Folder structure

```
Algo-Trade-Delta/
├── config.py             # All config + symbol lot/limit settings (env-driven)
├── .env.example          # Copy to .env and fill in your API credentials
├── requirements.txt      # Python dependencies
├── database.py           # SQLite persistence (state, trades, metrics, audit)
├── delta_client.py       # Delta Exchange API client (auth, retries, backoff)
├── heiken_ashi.py        # Heiken Ashi candle calculations
├── logger.py             # Rotating structured logging
├── position_manager.py   # Position state, execution, validation, reconciliation
├── strategy.py           # Heiken Ashi entry / scale / reversal strategy
├── main.py               # Entry point + run loop + graceful shutdown
├── trading.db            # SQLite database (created on first run)
├── logs/                 # Rotating log files
│   └── algo_trade_delta.log
└── systemd/
    └── algo_trade_delta.service   # systemd unit for 24x7 execution on Ubuntu
```

## Quick start (local)

```bash
# 1. Create the project folder (already done) and enter it
cd Algo-Trade-Delta

# 2. Create a virtual environment
python3 -m venv venv
source venv/bin/activate        # Windows: venv\Scripts\activate

# 3. Install dependencies
pip install -r requirements.txt

# 4. Configure credentials
cp .env.example .env
# Edit .env and set DELTA_API_KEY, DELTA_API_SECRET

# 5. Run (paper trading by default)
python main.py
```

## AWS EC2 deployment

See **Deployment guide** below.

## Strategy in one paragraph

The bot evaluates the latest fully-closed 2-hour Heiken Ashi candle for each
configured symbol:
- GREEN close → open/add LONG (base_lot each, up to max_entries).
- RED close   → open/add SHORT (base_lot each, up to max_entries).
- Color change  → close all positions in the old direction, confirm closure
  on the exchange, reset entry count, then open base_lot in the new direction.

Each candle is processed exactly once (tracked in SQLite), so the bot is
restart-safe and never generates duplicate trades.

## Configuration

Edit `config.py` `SYMBOL_CONFIG` to add/change symbols. `max_position_size`
is derived automatically as `base_lot * max_entries`. No lot sizes or limits
are hardcoded elsewhere.

All credentials come from `.env` (environment variables) — never hardcoded.

## License

Educational / personal use. Trade at your own risk.
