#!/usr/bin/env python3
"""
Integrated VPS & Website Monitor
Monitors server health (CPU/RAM/Disk) AND Website availability.
Sends unified alerts to Telegram.
"""

import yaml
import os
import sys
import time
import signal
import logging
import datetime
import schedule
import requests
import paramiko
import json
import urllib.request
import argparse

# ── Configuration & Setup ──
# Initialise the logger.  The log‑file handler will be added
# later, after command‑line arguments are parsed, so that the
# user can enable file logging with `--log-file`.
logger = logging.getLogger("VPSMonitor")
logger.setLevel(logging.INFO)
handler = logging.StreamHandler()
formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)

# ── Helper Functions ──
def load_config(config_path="config.yaml"):
    """Load configuration from YAML file."""
    if not os.path.exists(config_path):
        print(f"Error: Config file '{config_path}' not found.")
        sys.exit(1)
    with open(config_path, 'r') as f:
        return yaml.safe_load(f)

def send_telegram(config, message):
    """Send formatted message to Telegram."""
    try:
        token = config["telegram"]["bot_token"]
        chat_id = config["telegram"]["chat_id"]
        url = f"https://api.telegram.org/bot{token}/sendMessage"

        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "Markdown"
        }

        requests.post(url, data=payload, timeout=10)
    except Exception as e:
        logger.error(f"Failed to send Telegram alert: {e}")

def ssh_connect(server):
    """Establish SSH connection."""
    host = server['host']
    port = server.get('port', 22)
    username = server['user']
    password = server.get('password')
    key_path = server.get('key_path')

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())

    if key_path:
        try:
            pkey = paramiko.RSAKey.from_private_key_file(key_path)
            client.connect(hostname=host, port=port, username=username, pkey=pkey, timeout=10)
        except Exception as e:
            logger.error(f"SSH Key Auth failed for {host}: {e}")
            raise e
    elif password:
        client.connect(hostname=host, port=port, username=username, password=password, timeout=10)
    else:
        raise ValueError(f"No auth method defined for {host}")

    return client

def run_command(client, command):
    """Execute a command on remote server."""
    stdin, stdout, stderr = client.exec_command(command)
    return stdout.read().decode().strip()

def run_vps_checks(servers, thresholds, config):
    """Check VPS health and return results list."""
    results = []
    now = datetime.datetime.now().strftime("%d %b %Y, %H:%M:%S")

    for server in servers:
        name = server.get("name", server["host"])
        up = False
        data = {}
        client = None  # Ensure client is defined

        try:
            client = ssh_connect(server)

            # Collect Metrics
            uptime = run_command(client, "uptime -p").split()[0]

            # CPU
            try:
                cpu_load = run_command(client, "grep 'cpu(s)' /proc/loadavg | awk '{print $2}'")
                cpu_percent = float(cpu_load)
            except Exception:
                cpu_percent = 0.0

            # RAM
            try:
                ram_total = int(run_command(client, "grep MemTotal /proc/meminfo | awk '{print $2}'")) / 1024
                ram_free = int(run_command(client, "grep MemAvailable /proc/meminfo | awk '{print $2}'")) / 1024
                ram_percent = round(((ram_total - ram_free) / ram_total) * 100, 2)
            except Exception:
                ram_percent = 0.0

            data = {
                "uptime": uptime,
                "cpu_percent": cpu_percent,
                "ram_percent": ram_percent
            }

            # Check thresholds
            alerts = []
            if cpu_percent > thresholds.get("cpu_percent", 80):
                alerts.append(f"- CPU: {cpu_percent}% (Limit: {thresholds.get('cpu_percent')})")
            if ram_percent > thresholds.get("ram_percent", 80):
                alerts.append(f"- RAM: {ram_percent:.1f}% (Limit: {thresholds.get('ram_percent')})")

            if alerts:
                alert_msg = f"⚠️ *VPS Alert: {name}*\n🕐 {now}\n" + "\n".join(alerts)
                send_telegram(config, alert_msg)

            print(f"[✓] {name} is UP | CPU: {cpu_percent:.1f}% | RAM: {ram_percent:.1f}% | Uptime: {uptime}")
            results.append((name, True, data))

        except Exception as e:
            logger.error(f"Failed to check {name}: {e}")
            print(f"[✗] {name} is DOWN | Error: {e}")
            results.append((name, False, {"error": str(e)}))

        finally:
            if client is not None:
                client.close()

    return results

def run_website_checks(sites_list, config):
    """Check website health and return results."""
    results = []
    now = datetime.datetime.now().strftime("%d %b %Y, %H:%M:%S")

    # Default headers that mimic a real browser
    default_headers = {
        "User-Agent": "Mozilla/5.0 (compatible; VPSMonitor/1.0; +https://yourdomain.com)",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Connection": "keep-alive",
        "Accept-Encoding": "gzip, deflate, br",
    }

    for site in sites_list:
        url = site
        name = site
        status_code = 200
        response_time = 0
        status = "up"
        error_msg = ""

        try:
            start = time.time()
            resp = requests.get(url, headers=default_headers, timeout=10)
            response_time = time.time() - start
            status_code = resp.status_code
            resp.raise_for_status()
        except requests.exceptions.RequestException as e:
            if isinstance(e, requests.exceptions.ConnectionError):
                status = "offline"
                status_code = 0
                error_msg = "Connection Refused/Timeout"
            elif isinstance(e, requests.exceptions.HTTPError):
                status = "error"
                status_code = e.response.status_code
                error_msg = e.response.reason
            else:
                status = "error"
                status_code = 0
                error_msg = str(e)

        # Alert if non‑200
        if status_code != 200:
            alert_text = f"🔴 *Website Alert: {name}*\n🕐 {now}\n🔗 {url}\nCode: {status_code} ({error_msg})\nTime: {response_time:.2f}s\n"
            send_telegram(config, alert_text)

        if status_code == 200:
            print(f"[✓] {name} is UP | Code: 200 | Time: {response_time:.2f}s")
        else:
            print(f"[✗] {name} is DOWN | Code: {status_code} | Error: {error_msg}")

        results.append({
            "name": name,
            "url": url,
            "status": status,
            "code": status_code,
            "time": response_time,
            "error": error_msg
        })

    return results

def format_status_block(item):
    name = item["name"]
    is_up = item.get("status", True)

    if not is_up:
        return f"*❌ {name}*\n{item.get('error', 'Connection Lost')}"

    if "server_data" in item:
        data = item["server_data"]
        return f"*✅ {name}*\n⏳ Uptime: {data.get('uptime', '-')}\n💻 CPU: {data.get('cpu_percent', '-')}%\n✅ Online"

    # Website block
    return f"*✅ {name}*\n🔗 {item.get('url', '')}\n✅ HTTP {item.get('code', 200)}"

def run_checks(config):
    vps_results = run_vps_checks(config["servers"], config["thresholds"], config)
    website_results = run_website_checks(config.get("websites", []), config)

    all_results = []
    for name, is_up, data in vps_results:
        all_results.append({"name": name, "is_up": is_up, "data": data})
    for item in website_results:
        all_results.append({"name": item["name"], "is_up": item["status"] == "up", "data": item})

    if config.get("send_summary", True):
        now = datetime.datetime.now().strftime("%d %b %Y, %H:%M:%S")
        header = f"📡 *VPS & Website Status Report*\n🕐 _{now}_\n\n"
        online = sum(1 for r in all_results if r["is_up"])
        total = len(all_results)
        down = total - online
        header += f"📊 *Summary*\n✅ {online} Online"
        if down:
            header += f" | 🔴 {down} Down"
        header += "\n" + "-" * 30 + "\n"

        blocks = []
        for it in all_results:
            if it["is_up"]:
                blocks.append(format_status_block(it))
            else:
                blocks.append(f"*🔴 {it['name']}*\nError: {it.get('error', 'Connection Lost')}")

        send_telegram(config, header + "\n\n".join(blocks))

    return all_results

def run_test(config):
    """Test config, Telegram, and connections."""
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
            send_telegram(config, "🧪 *VPS Monitor Test*\n\nTelegram connection successful!")
        else:
            print(f"   ❌ Bot token invalid")
            return
    except Exception as e:
        print(f"   ❌ Telegram error: {e}")
        return

    # Test Servers
    print(f"\n🖥 Testing {len(config['servers'])} server(s)...\n")
    for server in config["servers"]:
        name = server.get("name", server["host"])
        try:
            client = ssh_connect(server)
            hostname = run_command(client, "hostname")
            print(f"   ✅ [{name}] Hostname: {hostname}")
            client.close()
        except Exception as e:
            print(f"   ❌ [{name}] Failed: {e}")

    # Test Websites
    print(f"\n🌐 Testing {len(config.get('websites', []))} website(s)...\n")
    for site in config.get("websites", []):
        try:
            resp = requests.get(site["url"], timeout=10)
            print(f"   ✅ [{site.get('site_name', site['url'])}] Status: {resp.status_code}")
        except Exception as e:
            print(f"   ❌ [{site.get('site_name', site['url'])}] Error: {e}")

    print("\n" + "=" * 50)
    print("  Test complete!")
    print("=" * 50 + "\n")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Integrated Monitor")
    parser.add_argument("--test", action="store_true", help="Run test mode")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--log-file", help="Write logs to the specified file")
    args = parser.parse_args()
    cfg = load_config()

    # Optional log file
    if args.log_file:
        file_handler = logging.FileHandler(args.log_file)
        file_handler.setFormatter(formatter)
        logger.addHandler(file_handler)
        logger.info(f"Logging to file: {args.log_file}")

    if args.test:
        run_test(cfg)
    elif args.once:
        run_checks(cfg)
    else:
        interval = cfg.get("check_interval_minutes", 10)
        send_telegram(cfg, f"🟢 *Monitor Started*\nServers: {len(cfg['servers'])}\nWebsites: {len(cfg.get('websites', []))}\nInterval: {interval} min.")
        run_checks(cfg)
        import schedule
        schedule.every(interval).minutes.do(run_checks, cfg)

        def _shutdown(signum, frame):
            send_telegram(cfg, "🔴 *Monitor Stopped*")
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        while True:
            schedule.run_pending()
            time.sleep(1)