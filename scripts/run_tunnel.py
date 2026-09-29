#!/usr/bin/env python3
"""Run Cloudflare Quick Tunnel and automatically sync MINI_APP_PUBLIC_URL.

When cloudflared starts, it assigns a dynamic trycloudflare.com domain.
This script extracts that domain, waits for the health check to succeed,
writes it to .env if changed, and restarts floorball-content-bot.service
so Telegram's Chat Menu Button stays up-to-date.
"""

from __future__ import annotations

import ipaddress
import logging
import os
from pathlib import Path
import re
import signal
import subprocess
import sys
import time
from urllib.error import URLError
from urllib.request import urlopen

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] [floorball-tunnel] %(message)s",
)
logger = logging.getLogger(__name__)

TUNNEL_RE = re.compile(r"https://([a-zA-Z0-9-]+\.trycloudflare\.com)")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
ENV_PATH = PROJECT_ROOT / ".env"
CLOUDFLARED_BIN = Path(os.environ.get("CLOUDFLARED_BIN", Path.home() / ".local/bin/cloudflared"))
ENV_PATTERN = re.compile(r"^MINI_APP_PUBLIC_URL=.*$", re.MULTILINE)


def wait_for_health(public_url: str) -> bool:
    hostname = public_url.removeprefix("https://").rstrip("/")
    for _ in range(25):
        try:
            with urlopen(f"{public_url.rstrip('/')}/healthz", timeout=5) as response:
                if response.status == 200:
                    return True
        except (OSError, URLError):
            pass

        # If local resolver has NXDOMAIN cached, resolve against Cloudflare 1.1.1.1
        try:
            lookup = subprocess.run(
                ["dig", "+short", hostname, "@1.1.1.1"],
                capture_output=True,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            time.sleep(2)
            continue

        if lookup.returncode == 0:
            for value in lookup.stdout.splitlines():
                try:
                    address = ipaddress.IPv4Address(value.strip())
                except ipaddress.AddressValueError:
                    continue
                try:
                    checked = subprocess.run(
                        [
                            "curl",
                            "--fail",
                            "--silent",
                            "--show-error",
                            "--max-time",
                            "8",
                            "--resolve",
                            f"{hostname}:443:{address}",
                            "--output",
                            "/dev/null",
                            f"{public_url.rstrip('/')}/healthz",
                        ],
                        capture_output=True,
                        text=True,
                        check=False,
                        timeout=10,
                    )
                except (OSError, subprocess.TimeoutExpired):
                    continue
                if checked.returncode == 0:
                    return True
        time.sleep(2)
    return False


def update_env(new_url: str) -> bool:
    if not ENV_PATH.exists():
        logger.warning(".env file not found at %s", ENV_PATH)
        return False
    content = ENV_PATH.read_text(encoding="utf-8")
    formatted_url = new_url.rstrip("/") + "/"
    replacement = f"MINI_APP_PUBLIC_URL={formatted_url}"
    match = re.search(r"^MINI_APP_PUBLIC_URL=(.*)$", content, flags=re.MULTILINE)
    current_url = match.group(1).strip() if match else ""
    if current_url == formatted_url:
        logger.info("MINI_APP_PUBLIC_URL already set to %s", formatted_url)
        return False

    if ENV_PATTERN.search(content):
        updated = ENV_PATTERN.sub(replacement, content)
    else:
        updated = content.rstrip("\n") + "\n" + replacement + "\n"

    tmp_path = PROJECT_ROOT / ".env.tunnel.tmp"
    try:
        with tmp_path.open("w", encoding="utf-8") as handle:
            handle.write(updated)
            handle.flush()
            os.fsync(handle.fileno())
        tmp_path.chmod(0o600)
        tmp_path.replace(ENV_PATH)
    finally:
        tmp_path.unlink(missing_ok=True)

    logger.info("Updated MINI_APP_PUBLIC_URL in %s to %s", ENV_PATH, formatted_url)
    return True


def restart_bot_service() -> None:
    logger.info("Restarting floorball-content-bot.service to apply new Mini App URL...")
    try:
        res = subprocess.run(
            ["systemctl", "--user", "restart", "floorball-content-bot.service"],
            capture_output=True,
            text=True,
            timeout=30,
            check=False,
        )
        if res.returncode == 0:
            logger.info("floorball-content-bot.service restarted successfully.")
        else:
            logger.warning(
                "Failed to restart floorball-content-bot.service (code %d): %s",
                res.returncode,
                res.stderr.strip(),
            )
    except Exception as exc:
        logger.exception("Error restarting floorball-content-bot.service: %s", exc)


def main() -> int:
    if not CLOUDFLARED_BIN.is_file() or not os.access(CLOUDFLARED_BIN, os.X_OK):
        logger.error("cloudflared is missing or not executable at %s", CLOUDFLARED_BIN)
        return 1

    cmd = [
        str(CLOUDFLARED_BIN),
        "tunnel",
        "--url",
        "http://127.0.0.1:8092",
        "--no-autoupdate",
    ]
    logger.info("Starting cloudflared: %s", " ".join(cmd))
    proc = subprocess.Popen(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )

    def handle_signal(signum: int, _frame: object) -> None:
        logger.info("Received signal %d, terminating cloudflared...", signum)
        proc.terminate()

    signal.signal(signal.SIGTERM, handle_signal)
    signal.signal(signal.SIGINT, handle_signal)

    url_synced = False
    try:
        assert proc.stdout is not None
        for raw_line in iter(proc.stdout.readline, ""):
            sys.stdout.write(raw_line)
            sys.stdout.flush()

            if not url_synced:
                match = TUNNEL_RE.search(raw_line)
                if match:
                    detected_url = match.group(0)
                    logger.info("Detected Cloudflare Quick Tunnel URL: %s", detected_url)
                    if wait_for_health(detected_url):
                        changed = update_env(detected_url)
                        if changed:
                            restart_bot_service()
                        url_synced = True
                    else:
                        logger.warning("Health check timed out for %s", detected_url)
    finally:
        if proc.poll() is None:
            proc.terminate()
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                proc.kill()

    return proc.returncode or 0


if __name__ == "__main__":
    sys.exit(main())
