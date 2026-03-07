#!/usr/bin/env python3
"""
VPS Server Monitor
==================
Monitors multiple VPS servers via SSH for CPU, RAM, disk usage,
load average, and uptime. Sends Telegram notifications every cycle
with status summaries and threshold-based alerts.

Usage:
    python vps_monitor.py              # Start monitoring loop
    python vps_monitor.py --test       # Test config, SSH, and Telegram
    python vps_monitor.py --once       # Run one check and exit
"""

import os
import sys
import time
import signal
import logging
import argparse
from datetime import datetime, timedelta
from pathlib import Path

import yaml
import paramiko
import requests
import schedule


# ── Logging Setup ────────────────────────────────────────────────

LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler(LOG_DIR / "vps_monitor.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger("vps_monitor")


# ── Config ───────────────────────────────────────────────────────

CONFIG_PATH = Path(__file__).parent / "config.yaml"


def load_config():
    """Load and validate config.yaml."""
    if not CONFIG_PATH.exists():
        logger.error(f"Config file not found: {CONFIG_PATH}")
        sys.exit(1)

    with open(CONFIG_PATH, "r") as f:
        config = yaml.safe_load(f)

    # Validate required fields
    tg = config.get("telegram", {})
    if not tg.get("bot_token") or tg["bot_token"] == "YOUR_BOT_TOKEN_HERE":
        logger.error("Telegram bot_token not configured in config.yaml")
        sys.exit(1)
    if not tg.get("chat_id") or str(tg["chat_id"]) == "YOUR_CHAT_ID_HERE":
        logger.error("Telegram chat_id not configured in config.yaml")
        sys.exit(1)

    servers = config.get("servers", [])
    if not servers:
        logger.error("No servers configured in config.yaml")
        sys.exit(1)

    return config


# ── Telegram ─────────────────────────────────────────────────────

def send_telegram(config, message):
    """Send a message via Telegram Bot API."""
    token = config["telegram"]["bot_token"]
    chat_id = config["telegram"]["chat_id"]
    url = f"https://api.telegram.org/bot{token}/sendMessage"

    # Telegram max message length is 4096
    chunks = [message[i:i + 4000] for i in range(0, len(message), 4000)]

    for chunk in chunks:
        try:
            resp = requests.post(url, json={
                "chat_id": chat_id,
                "text": chunk,
                "parse_mode": "Markdown",
            }, timeout=30)

            if not resp.ok:
                # Retry without markdown if parsing fails
                resp2 = requests.post(url, json={
                    "chat_id": chat_id,
                    "text": chunk,
                }, timeout=30)
                if not resp2.ok:
                    logger.error(f"Telegram send failed: {resp2.text}")
        except Exception as e:
            logger.error(f"Telegram error: {e}")


# ── SSH Metrics Collection ───────────────────────────────────────

def ssh_connect(server):
    """Create an SSH connection to a server. Returns paramiko.SSHClient or None."""
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    host = server["host"]
    port = server.get("port", 22)
    user = server.get("user", "root")
    password = server.get("password")
    key_path = server.get("key_path")

    connect_kwargs = {
        "hostname": host,
        "port": port,
        "username": user,
        "timeout": 15,
        "banner_timeout": 15,
        "auth_timeout": 15,
    }

    if key_path:
        expanded = os.path.expanduser(key_path)
        connect_kwargs["key_filename"] = expanded
    elif password:
        connect_kwargs["password"] = password

    client.connect(**connect_kwargs)
    return client


def run_command(client, cmd):
    """Execute a command on the SSH client and return stdout."""
    _, stdout, stderr = client.exec_command(cmd, timeout=10)
    return stdout.read().decode("utf-8", errors="replace").strip()


def collect_metrics(client):
    """Collect system metrics from an SSH connection."""
    metrics = {}

    # ── CPU Usage (from /proc/stat, two-sample method) ──
    try:
        cpu_cmd = (
            "cat /proc/stat | head -1 && sleep 0.5 && cat /proc/stat | head -1"
        )
        output = run_command(client, cpu_cmd)
        lines = output.strip().split("\n")
        if len(lines) >= 2:
            vals1 = list(map(int, lines[0].split()[1:]))
            vals2 = list(map(int, lines[1].split()[1:]))
            idle1 = vals1[3] + (vals1[4] if len(vals1) > 4 else 0)
            idle2 = vals2[3] + (vals2[4] if len(vals2) > 4 else 0)
            total1 = sum(vals1)
            total2 = sum(vals2)
            total_diff = total2 - total1
            idle_diff = idle2 - idle1
            if total_diff > 0:
                metrics["cpu_percent"] = round(
                    (1 - idle_diff / total_diff) * 100, 1
                )
            else:
                metrics["cpu_percent"] = 0.0
        else:
            metrics["cpu_percent"] = None
    except Exception as e:
        logger.debug(f"CPU collection error: {e}")
        metrics["cpu_percent"] = None

    # ── RAM Usage (from free -m) ──
    try:
        output = run_command(client, "free -m")
        for line in output.split("\n"):
            if line.startswith("Mem:"):
                parts = line.split()
                total = int(parts[1])
                available = int(parts[6]) if len(parts) >= 7 else int(parts[3])
                used = total - available
                metrics["ram_total_mb"] = total
                metrics["ram_used_mb"] = used
                metrics["ram_percent"] = round((used / total) * 100, 1) if total > 0 else 0
                break
    except Exception as e:
        logger.debug(f"RAM collection error: {e}")
        metrics["ram_total_mb"] = None
        metrics["ram_used_mb"] = None
        metrics["ram_percent"] = None

    # ── Disk Usage (from df -h /) ──
    try:
        output = run_command(client, "df -h / | tail -1")
        parts = output.split()
        if len(parts) >= 5:
            metrics["disk_total"] = parts[1]
            metrics["disk_used"] = parts[2]
            metrics["disk_available"] = parts[3]
            metrics["disk_percent"] = float(parts[4].replace("%", ""))
        else:
            metrics["disk_percent"] = None
    except Exception as e:
        logger.debug(f"Disk collection error: {e}")
        metrics["disk_percent"] = None

    # ── Load Average ──
    try:
        output = run_command(client, "cat /proc/loadavg")
        parts = output.split()
        metrics["load_1"] = float(parts[0])
        metrics["load_5"] = float(parts[1])
        metrics["load_15"] = float(parts[2])
    except Exception as e:
        logger.debug(f"Load collection error: {e}")
        metrics["load_1"] = None

    # ── CPU Cores (for load context) ──
    try:
        output = run_command(client, "nproc")
        metrics["cpu_cores"] = int(output)
    except Exception:
        metrics["cpu_cores"] = None

    # ── Uptime ──
    try:
        output = run_command(client, "uptime -p")
        metrics["uptime"] = output.replace("up ", "").strip()
    except Exception:
        metrics["uptime"] = None

    # ── Running Processes ──
    try:
        output = run_command(client, "ps aux --no-heading | wc -l")
        metrics["processes"] = int(output)
    except Exception:
        metrics["processes"] = None

    return metrics


def check_server(server):
    """Check a single server. Returns (server_name, is_up, metrics_or_error)."""
    name = server.get("name", server["host"])

    try:
        client = ssh_connect(server)
        try:
            metrics = collect_metrics(client)
            metrics["checked_at"] = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            return (name, True, metrics)
        finally:
            client.close()
    except paramiko.AuthenticationException:
        return (name, False, "Authentication failed (bad password/key)")
    except paramiko.SSHException as e:
        return (name, False, f"SSH error: {str(e)[:100]}")
    except TimeoutError:
        return (name, False, "Connection timed out")
    except ConnectionRefusedError:
        return (name, False, "Connection refused (SSH port closed?)")
    except Exception as e:
        return (name, False, f"Error: {str(e)[:100]}")


# ── Formatters ───────────────────────────────────────────────────

def _bar(percent, width=10):
    """Create a visual percentage bar."""
    if percent is None:
        return "░" * width
    filled = int(percent / 100 * width)
    return "█" * filled + "░" * (width - filled)


def _status_emoji(percent, threshold):
    """Return emoji based on threshold."""
    if percent is None:
        return "⚪"
    if percent >= threshold:
        return "🔴"
    elif percent >= threshold * 0.8:
        return "🟡"
    return "🟢"


def format_server_status(name, metrics, thresholds):
    """Format a single server's status as a readable block."""
    cpu = metrics.get("cpu_percent")
    ram = metrics.get("ram_percent")
    disk = metrics.get("disk_percent")

    cpu_t = thresholds.get("cpu_percent", 80)
    ram_t = thresholds.get("ram_percent", 80)
    disk_t = thresholds.get("disk_percent", 85)

    lines = [f"🖥 *{name}*"]
    lines.append("─" * 24)

    # CPU
    if cpu is not None:
        lines.append(
            f"{_status_emoji(cpu, cpu_t)} CPU: `{cpu:5.1f}%` {_bar(cpu)}"
        )
    else:
        lines.append("⚪ CPU: `N/A`")

    # RAM
    if ram is not None:
        used = metrics.get("ram_used_mb", 0)
        total = metrics.get("ram_total_mb", 0)
        lines.append(
            f"{_status_emoji(ram, ram_t)} RAM: `{ram:5.1f}%` {_bar(ram)}  ({used}M/{total}M)"
        )
    else:
        lines.append("⚪ RAM: `N/A`")

    # Disk
    if disk is not None:
        used_d = metrics.get("disk_used", "?")
        total_d = metrics.get("disk_total", "?")
        lines.append(
            f"{_status_emoji(disk, disk_t)} Disk: `{disk:5.1f}%` {_bar(disk)}  ({used_d}/{total_d})"
        )
    else:
        lines.append("⚪ Disk: `N/A`")

    # Load
    load = metrics.get("load_1")
    cores = metrics.get("cpu_cores")
    if load is not None:
        load_str = (
            f"{metrics['load_1']:.2f} / {metrics.get('load_5', 0):.2f} / "
            f"{metrics.get('load_15', 0):.2f}"
        )
        core_info = f" ({cores} cores)" if cores else ""
        lines.append(f"📊 Load: `{load_str}`{core_info}")

    # Uptime & Processes
    uptime = metrics.get("uptime")
    procs = metrics.get("processes")
    extra = []
    if uptime:
        extra.append(f"⏱ {uptime}")
    if procs:
        extra.append(f"⚙️ {procs} procs")
    if extra:
        lines.append(" | ".join(extra))

    return "\n".join(lines)


def format_down_server(name, error):
    """Format a server-down block."""
    return (
        f"🖥 *{name}*\n"
        f"{'─' * 24}\n"
        f"🔴 *SERVER DOWN*\n"
        f"❌ {error}"
    )


# ── State Tracking ───────────────────────────────────────────────

# Track previous state for transition detection
_prev_states = {}  # server_name -> {"up": bool, "alerts": set()}


def detect_transitions(results, thresholds):
    """
    Compare current results against previous state.
    Returns (transition_alerts, threshold_alerts) — lists of alert strings.
    """
    global _prev_states
    transition_alerts = []
    threshold_alerts = []

    cpu_t = thresholds.get("cpu_percent", 80)
    ram_t = thresholds.get("ram_percent", 80)
    disk_t = thresholds.get("disk_percent", 85)

    for name, is_up, data in results:
        prev = _prev_states.get(name, {"up": None, "alerts": set()})

        if is_up:
            # Was down → now up (recovered)
            if prev["up"] is False:
                transition_alerts.append(f"✅ *{name}* — Recovered and back online!")

            # Check thresholds
            current_alerts = set()
            metrics = data
            cpu = metrics.get("cpu_percent")
            ram = metrics.get("ram_percent")
            disk = metrics.get("disk_percent")

            if cpu is not None and cpu >= cpu_t:
                current_alerts.add("cpu")
                if "cpu" not in prev.get("alerts", set()):
                    threshold_alerts.append(
                        f"🔴 *{name}* — CPU at *{cpu}%* (threshold: {cpu_t}%)"
                    )

            if ram is not None and ram >= ram_t:
                current_alerts.add("ram")
                if "ram" not in prev.get("alerts", set()):
                    threshold_alerts.append(
                        f"🔴 *{name}* — RAM at *{ram}%* (threshold: {ram_t}%)"
                    )

            if disk is not None and disk >= disk_t:
                current_alerts.add("disk")
                if "disk" not in prev.get("alerts", set()):
                    threshold_alerts.append(
                        f"🔴 *{name}* — Disk at *{disk}%* (threshold: {disk_t}%)"
                    )

            _prev_states[name] = {"up": True, "alerts": current_alerts}
        else:
            # Was up → now down
            if prev["up"] is not False:
                transition_alerts.append(
                    f"🚨 *{name}* — *SERVER DOWN!*\n   _{data}_"
                )
            _prev_states[name] = {"up": False, "alerts": set()}

    return transition_alerts, threshold_alerts


# ── Main Monitor Logic ───────────────────────────────────────────

def run_checks(config):
    """Run a single monitoring cycle."""
    servers = config["servers"]
    thresholds = config.get("thresholds", {})
    send_summary = config.get("send_summary", True)

    logger.info(f"Checking {len(servers)} server(s)...")

    results = []
    for server in servers:
        name, is_up, data = check_server(server)
        status = "UP" if is_up else f"DOWN ({data})"
        logger.info(f"  {name}: {status}")
        results.append((name, is_up, data))

    # Detect transitions (down/recovered/threshold breaches)
    transition_alerts, threshold_alerts = detect_transitions(results, thresholds)

    # ── Send transition alerts immediately ──
    if transition_alerts:
        alert_msg = "🚨 *VPS Alert!*\n\n" + "\n\n".join(transition_alerts)
        send_telegram(config, alert_msg)

    if threshold_alerts:
        alert_msg = "⚠️ *Threshold Warning!*\n\n" + "\n\n".join(threshold_alerts)
        send_telegram(config, alert_msg)

    # ── Send periodic summary ──
    if send_summary:
        now = datetime.now().strftime("%d %b %Y, %H:%M:%S")
        header = f"📡 *VPS Status Report*\n🕐 _{now}_\n"

        up_count = sum(1 for _, up, _ in results if up)
        down_count = len(results) - up_count
        header += f"✅ {up_count} Online"
        if down_count:
            header += f" | 🔴 {down_count} Down"
        header += "\n"

        blocks = []
        for name, is_up, data in results:
            if is_up:
                blocks.append(format_server_status(name, data, thresholds))
            else:
                blocks.append(format_down_server(name, data))

        full_msg = header + "\n" + "\n\n".join(blocks)
        send_telegram(config, full_msg)

    logger.info("Check cycle complete.")
    return results


# ── Test Mode ────────────────────────────────────────────────────

def run_test(config):
    """Test config, Telegram, and SSH connections."""
    print("\n" + "=" * 50)
    print("  VPS Monitor — Connection Test")
    print("=" * 50)

    # Test Telegram
    print("\n📱 Testing Telegram...")
    try:
        token = config["telegram"]["bot_token"]
        chat_id = config["telegram"]["chat_id"]
        url = f"https://api.telegram.org/bot{token}/getMe"
        resp = requests.get(url, timeout=10)
        if resp.ok:
            bot_name = resp.json().get("result", {}).get("first_name", "Unknown")
            print(f"   ✅ Bot connected: {bot_name}")

            # Send test message
            send_telegram(config, "🧪 *VPS Monitor Test*\n\nTelegram connection successful!")
            print(f"   ✅ Test message sent to chat {chat_id}")
        else:
            print(f"   ❌ Bot token invalid: {resp.text[:100]}")
            return
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")
        return

    # Test SSH to each server
    print(f"\n🖥 Testing {len(config['servers'])} server(s)...\n")
    for server in config["servers"]:
        name = server.get("name", server["host"])
        print(f"   [{name}] Connecting to {server['host']}:{server.get('port', 22)}...")

        try:
            client = ssh_connect(server)
            hostname = run_command(client, "hostname")
            os_info = run_command(client, "cat /etc/os-release | head -1")
            client.close()
            print(f"   ✅ Connected! Hostname: {hostname}")
            print(f"      OS: {os_info}")
        except Exception as e:
            print(f"   ❌ Failed: {e}")

    print("\n" + "=" * 50)
    print("  Test complete!")
    print("=" * 50 + "\n")


# ── Entry Point ──────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(description="VPS Server Monitor")
    parser.add_argument("--test", action="store_true", help="Test connections and exit")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    args = parser.parse_args()

    config = load_config()

    if args.test:
        run_test(config)
        return

    if args.once:
        run_checks(config)
        return

    # ── Scheduled loop ──
    interval = config.get("check_interval_minutes", 10)
    logger.info(f"VPS Monitor started — checking every {interval} minutes")
    logger.info(f"Monitoring {len(config['servers'])} server(s)")

    # Notify on startup
    send_telegram(
        config,
        f"🟢 *VPS Monitor Started*\n"
        f"Monitoring {len(config['servers'])} server(s) every {interval} min.",
    )

    # Run first check immediately
    run_checks(config)

    # Schedule recurring checks
    schedule.every(interval).minutes.do(run_checks, config)

    # Graceful shutdown
    def _shutdown(signum, frame):
        logger.info("Shutting down...")
        send_telegram(config, "🔴 *VPS Monitor Stopped*")
        sys.exit(0)

    signal.signal(signal.SIGINT, _shutdown)
    signal.signal(signal.SIGTERM, _shutdown)

    # Main loop
    while True:
        schedule.run_pending()
        time.sleep(1)


if __name__ == "__main__":
    main()
