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
import subprocess
from logging.handlers import RotatingFileHandler, TimedRotatingFileHandler

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

# ── Logging Setup Functions ──
def setup_logging(args):
    """Set up logging with rotating file handler."""
    # Create logs directory if it doesn't exist
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)
    
    # Set log file path
    log_file_path = os.path.join(log_dir, "monitor.log")
    
    # Remove existing handlers to avoid duplication
    logger.handlers = []
    
    # Console handler (always enabled)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_formatter = logging.Formatter('%(asctime)s - %(levelname)s - %(message)s')
    console_handler.setFormatter(console_formatter)
    logger.addHandler(console_handler)
    
    # Rotating file handler (rotate when file reaches 5MB)
    max_bytes = 5 * 1024 * 1024  # 5MB
    backup_count = 5  # Keep last 5 rotated files
    file_handler = RotatingFileHandler(
        log_file_path,
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding='utf-8'
    )
    file_handler.setLevel(logging.DEBUG)
    file_formatter = logging.Formatter(
        '%(asctime)s - %(name)s - %(levelname)s - %(funcName)s:%(lineno)d - %(message)s'
    )
    file_handler.setFormatter(file_formatter)
    logger.addHandler(file_handler)
    
    logger.info(f"Logging configured. File: {log_file_path}")
    logger.debug(f"Max log file size: {max_bytes / 1024 / 1024:.1f}MB")
    logger.debug(f"Backup files retained: {backup_count}")

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
            uptime = run_command(client, "uptime -p")

            try:
                # Get CPU usage percentage. top -bn1 gives a one-shot snapshot.
                # We extract the idle percentage and subtract from 100.
                cpu_load = run_command(client, "top -bn1 | grep 'Cpu(s)' | sed 's/.*, *\\([0-9.]*\\)%* id.*/\\1/' | awk '{print 100 - $1}'")
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

            # Disk Usage
            try:
                disk_output = run_command(client, "df -h / | tail -1 | awk '{print $5}'")
                disk_percent = float(disk_output.replace('%', ''))
                # Also get total and used for detailed reporting
                disk_detail = run_command(client, "df -h / | tail -1")
                parts = disk_detail.split()
                if len(parts) >= 2:
                    disk_total = parts[1]
                    disk_used = parts[2]
                    data["disk_detail"] = f"{disk_used} / {disk_total}"
            except Exception:
                disk_percent = 0.0
                data["disk_detail"] = "-"

            data = {
                "uptime": uptime,
                "cpu_percent": cpu_percent,
                "ram_percent": ram_percent,
                "disk_percent": disk_percent
            }

            # Check thresholds
            alerts = []
            top_cpu_output = ""
            top_ram_output = ""

            report_processes = thresholds.get("report_top_processes", True)
            process_count = thresholds.get("top_processes_count", 5) + 1 # +1 for header

            if cpu_percent > thresholds.get("cpu_percent", 80):
                alerts.append(f"- CPU: {cpu_percent}% (Limit: {thresholds.get('cpu_percent')}%)")
                if report_processes:
                    try:
                        cmd = f"ps -eo pid,user,%cpu,%mem,cmd --sort=-%cpu | head -n {process_count} | cut -c 1-70"
                        top_cpu_output = run_command(client, cmd)
                    except Exception as e:
                        top_cpu_output = f"Error fetching processes: {e}"

            if ram_percent > thresholds.get("ram_percent", 80):
                alerts.append(f"- RAM: {ram_percent:.1f}% (Limit: {thresholds.get('ram_percent')}%)")
                if report_processes:
                    try:
                        cmd = f"ps -eo pid,user,%cpu,%mem,cmd --sort=-%mem | head -n {process_count} | cut -c 1-70"
                        top_ram_output = run_command(client, cmd)
                    except Exception as e:
                        top_ram_output = f"Error fetching processes: {e}"

            if disk_percent > thresholds.get("disk_percent", 50):
                alerts.append(f"- Disk: {disk_percent}% (Limit: {thresholds.get('disk_percent')}%)")

            if alerts:
                alert_msg = f"⚠️ *VPS Alert: {name}*\n🕐 {now}\n" + "\n".join(alerts)
                if top_cpu_output:
                    alert_msg += f"\n\n🔥 *Top CPU Processes:*\n```\n{top_cpu_output}\n```"
                if top_ram_output:
                    alert_msg += f"\n\n🧠 *Top RAM Processes:*\n```\n{top_ram_output}\n```"
                
                send_telegram(config, alert_msg)
                logger.warning(f"Alert sent for {name}: {' | '.join(alerts)}")

            # Log success
            log_msg = (
                f"[✓] {name} is UP | "
                f"CPU: {cpu_percent:.1f}% | "
                f"RAM: {ram_percent:.1f}% | "
                f"Disk: {disk_percent}% | "
                f"Uptime: {uptime}"
            )
            logger.info(log_msg)
            print(log_msg)
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
            # Detect redirects that indicate domain issues
            if resp.history:
                orig_netloc = urllib.parse.urlparse(url).netloc
                final_netloc = urllib.parse.urlparse(resp.url).netloc
                if not (
                    urllib.parse.urlparse(url).scheme == "http"
                    and urllib.parse.urlparse(resp.url).scheme == "https"
                ):
                    status = "error"
                    error_msg = f"Redirected to {resp.url}"
                    status_code = resp.status_code
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
            logger.warning(f"Website alert: {name} - {status_code} - {error_msg}")
        else:
            log_msg = (
                f"[✓] {name} is UP | "
                f"Code: 200 | "
                f"Time: {response_time:.2f}s"
            )
            logger.info(log_msg)
            print(log_msg)

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
        return f"*✅ {name}*\n⏳ Uptime: {data.get('uptime', '-')}\n💻 CPU: {data.get('cpu_percent', '-')}%\n🧠 RAM: {data.get('ram_percent', '-')}%\n💾 Disk: {data.get('disk_percent', '-')}%\n✅ Online"

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
        logger.info(f"Summary report sent: {online} online, {down} down")

    # Log completion
    logger.info("✓ Check cycle completed successfully")

    return all_results

def check_for_updates(config):
    """Fetch latest updates from git and restart the script if changes are detected."""
    logger.info("Checking for script updates from git...")
    try:
        # Run git fetch first
        subprocess.run(["git", "fetch", "origin"], check=True, capture_output=True, text=True)
        
        # Run git pull
        result = subprocess.run(["git", "pull", "origin"], check=True, capture_output=True, text=True)
        
        output = result.stdout.strip()
        if "Already up to date." not in output:
            logger.info("Updates pulled successfully. Restarting script...")
            send_telegram(config, "🔄 *Monitor Auto-Update*\nNew updates fetched from git.\nRestarting monitor script...")
            
            # Allow a moment for the telegram message to be sent
            time.sleep(2)
            
            # Restart the script
            os.execv(sys.executable, ['python3'] + sys.argv)
        else:
            logger.debug("No updates found.")
            
    except subprocess.CalledProcessError as e:
        logger.error(f"Failed to check for updates: {e.stderr or e.stdout}")
    except Exception as e:
        logger.error(f"Error during auto update: {e}")

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
    parser = argparse.ArgumentParser(description="VPS & Website Monitor")
    parser.add_argument("--test", action="store_true", help="Run test mode")
    parser.add_argument("--once", action="store_true", help="Run one check and exit")
    parser.add_argument("--log-file", help="Write logs to the specified file (overrides default)")
    args = parser.parse_args()
    cfg = load_config()

    # Setup logging (creates logs/ directory and configures handlers)
    setup_logging(args)

    # Optional custom log file path
    if args.log_file:
        logger.info(f"Using custom log file: {args.log_file}")

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
        
        # Check for self-updates every 60 minutes
        schedule.every(10).minutes.do(check_for_updates, cfg)

        def _shutdown(signum, frame):
            logger.info(f"Received signal {signum}. Shutting down gracefully...")
            send_telegram(cfg, "🔴 *Monitor Stopped*\nReason: Signal received (Ctrl+C or system shutdown)")
            sys.exit(0)

        signal.signal(signal.SIGINT, _shutdown)
        signal.signal(signal.SIGTERM, _shutdown)

        logger.info("🟢 Monitor is now running continuously...")
        logger.info("Press Ctrl+C to stop the monitor.")

        # Main loop - runs indefinitely
        while True:
            schedule.run_pending()
            time.sleep(1)
