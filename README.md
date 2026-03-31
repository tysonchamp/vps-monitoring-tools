# VPS Server Monitor 🖥️

A lightweight Python script that monitors your VPS servers via SSH and sends status reports + alerts through Telegram.

## Features

- **SSH-based** — no agent needed on your servers
- **CPU / RAM / Disk** usage with visual bars
- **Load average**, uptime, process count
- **Telegram alerts** — server down/recovered + threshold warnings
- **10-minute** monitoring cycle (configurable)
- **Systemd service** ready for production

## Quick Start

```bash
# 1. Install dependencies
pip install -r requirements.txt

# 2. Configure
cp config.yaml config.yaml.bak   # backup first time
nano config.yaml                  # add your servers + Telegram bot credentials

# 3. Test connections
python vps_monitor.py --test

# 4. Run once (single check)
python vps_monitor.py --once

# 5. Run loop (every 10 min)
python vps_monitor.py
```

## Configuration

Edit `config.yaml`:

| Field | Description |
|-------|-------------|
| `telegram.bot_token` | Get from [@BotFather](https://t.me/BotFather) |
| `telegram.chat_id` | Your personal chat ID (use [@userinfobot](https://t.me/userinfobot)) |
| `servers[].host` | VPS IP address |
| `servers[].user` | SSH username (usually `root`) |
| `servers[].password` | SSH password *or* use `key_path` |
| `servers[].key_path` | Path to SSH private key |
| `thresholds.*` | CPU/RAM/Disk % alert thresholds |

## Run as a Service

```bash
# Copy the service file
sudo cp system-monitor.service /etc/systemd/system/

# Edit paths if needed
sudo nano /etc/systemd/system/system-monitor.service

# Enable and start
sudo systemctl daemon-reload
sudo systemctl enable system-monitor
sudo systemctl start system-monitor

# Check status
sudo systemctl status system-monitor

# View logs
journalctl -u system-monitor -f
```

## Telegram Message Preview

```
📡 VPS Status Report
🕐 08 Mar 2026, 00:50:00
✅ 3 Online

🖥 Production VPS
────────────────────────
🟢 CPU:  12.3% ██░░░░░░░░
🟢 RAM:  45.2% █████░░░░░  (920M/2048M)
🟡 Disk: 72.0% ████████░░  (36G/50G)
📊 Load: 0.42 / 0.38 / 0.35 (2 cores)
⏱ 45 days, 3 hours | ⚙️ 128 procs
```
