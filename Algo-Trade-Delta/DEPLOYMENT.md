# AWS EC2 Deployment Guide — Algo-Trade-Delta

Target: **AWS EC2 Free Tier (Ubuntu)**, 24x7 execution via systemd.

## 1. Launch EC2 instance

- AMI: Ubuntu Server 22.04 LTS (Free Tier eligible: t2.micro / t3.micro)
- Instance type: t2.micro (or t3.micro)
- Storage: 8–30 GB gp2/gp3
- Security group: allow SSH (22) from your IP only. The bot only makes
  outbound HTTPS calls to Delta Exchange; no inbound ports needed beyond SSH.

Connect:
```bash
ssh -i your-key.pem ubuntu@<ec2-public-ip>
```

## 2. Install Python + git

```bash
sudo apt update && sudo apt upgrade -y
sudo apt install -y python3 python3-venv python3-pip git
```

## 3. Deploy the project

Copy the entire `Algo-Trade-Delta/` folder to the instance (e.g. via `scp`):

```bash
# From your local machine
scp -i your-key.pem -r C:\Users\new\Desktop\fyers_data\Algo-Trade-Delta ubuntu@<ec2-public-ip>:~/
```

On the instance:
```bash
cd ~/Algo-Trade-Delta
python3 -m venv venv
source venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

## 4. Configure credentials

```bash
cp .env.example .env
nano .env
```

Set at minimum:
```
DELTA_API_KEY=your_api_key
DELTA_API_SECRET=your_api_secret
DRY_RUN=True            # start in paper trading
LOG_LEVEL=INFO
CHECK_INTERVAL_MINUTES=60
```

> Never commit `.env`. It contains your API secret.

## 5. Database initialization

The database (`trading.db`) and all tables are created automatically on the
first run via `database.initialize()`. No manual migration is required.
If you want to reset, simply delete `trading.db` and restart.

## 6. Install as a systemd service (24x7)

```bash
sudo cp systemd/algo_trade_delta.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable algo_trade_delta.service
sudo systemctl start algo_trade_delta.service
```

Check status:
```bash
sudo systemctl status algo_trade_delta.service
```

View live logs:
```bash
sudo journalctl -u algo_trade_delta.service -f
```

Or read the rotating log file:
```bash
tail -f ~/Algo-Trade-Delta/logs/algo_trade_delta.log
```

## 7. Service commands

| Action | Command |
|--------|---------|
| Start | `sudo systemctl start algo_trade_delta` |
| Stop | `sudo systemctl stop algo_trade_delta` |
| Restart | `sudo systemctl restart algo_trade_delta` |
| Status | `sudo systemctl status algo_trade_delta` |
| Logs (live) | `sudo journalctl -u algo_trade_delta -f` |
| Enable on boot | `sudo systemctl enable algo_trade_delta` |
| Disable on boot | `sudo systemctl disable algo_trade_delta` |

## 8. Restart procedures

- Code change: edit files, then `sudo systemctl restart algo_trade_delta`
- Credential change: edit `.env`, then `sudo systemctl restart algo_trade_delta`
- Server reboot: service auto-starts on boot (enabled). State is recovered
  from SQLite + exchange reconciliation on startup (restart-safe).

## 9. Log monitoring

Rotating logs live in `logs/` (max 10 MB per file, 5 backups).

```bash
# Follow the application log
tail -f ~/Algo-Trade-Delta/logs/algo_trade_delta.log

# Search for errors
grep -i "error\|warning\|refused\|skipped" ~/Algo-Trade-Delta/logs/algo_trade_delta.log | tail -50

# systemd logs
sudo journalctl -u algo_trade_delta.service --since "1 hour ago"
```

## 10. Paper trading → live trading

1. Start with `DRY_RUN=True` (default). Orders are simulated; SQLite and logs
   are updated exactly as with live trades.
2. When confident, set `DRY_RUN=False` in `.env`.
3. Restart the service: `sudo systemctl restart algo_trade_delta`
4. Monitor closely for the first several candle cycles.

## 11. Safety notes

- The exchange is the source of truth for positions. On every startup the bot
  reconciles SQLite state against exchange positions and rebuilds local state
  on mismatch.
- Each candle is processed exactly once (tracked by `last_processed_candle_ts`).
- Position limits (`base_lot * max_entries`) are enforced before every order.
- Never hold LONG and SHORT simultaneously — reversals close fully before
  opening the opposite side.

## Troubleshooting

**Service won't start:** check `sudo journalctl -u algo_trade_delta.service -n 50`
and confirm `.env` has real credentials and `DRY_RUN` is set.

**No trades being placed:** confirm `DRY_RUN` setting, check logs for
"Scaling skipped" or "Max entries reached", and confirm a new 2-hour candle
has actually closed (bot wakes hourly).

**API errors / 401:** verify `DELTA_API_KEY` and `DELTA_API_SECRET` match your
Delta Exchange India account (https://www.delta.exchange/in/ → Settings → API).
