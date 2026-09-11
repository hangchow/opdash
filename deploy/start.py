#!/usr/bin/python3
"""Root-installed launcher; all repository code runs as the opdash user."""
import os
from pathlib import Path
import socket
import time


def main():
    host = os.environ.get("FUTU_HOST", "127.0.0.1")
    ports = [int(p) for p in os.environ.get("FUTU_PORTS", "11111").split(",")]
    attempt = 0
    while True:
        try:
            for port in ports:
                with socket.create_connection((host, port), timeout=2):
                    pass
            break
        except OSError:
            if attempt % 12 == 0:
                print("Waiting for local OpenD; retrying every 5 seconds", flush=True)
            attempt += 1
            time.sleep(5)
    release = Path("/opt/opdash/current").resolve(strict=True)
    os.environ["OPDASH_RELEASE"] = release.name
    args = [str(release / ".venv/bin/python"), "-u", str(release / "opdash_web.py")]
    if os.environ.get("STOCK_CODES"):
        args.append(os.environ["STOCK_CODES"])
    defaults = {
        "host": ("FUTU_HOST", "127.0.0.1"),
        "port": ("FUTU_PORTS", "11111"),
        "web_host": ("WEB_HOST", "192.168.10.1"),
        "web_port": ("WEB_PORT", "18080"),
        "poll_interval": ("POLL_INTERVAL", "10"),
        "price_interval": ("PRICE_INTERVAL", "10"),
        "ui_interval": ("UI_INTERVAL", "5"),
        "price_mode": ("PRICE_MODE", "auto"),
        "profit_highlight_threshold": ("PROFIT_HIGHLIGHT_THRESHOLD", "80"),
    }
    for flag, (key, default) in defaults.items():
        args.extend(["--" + flag, os.environ.get(key, default)])
    os.chdir(release)
    os.execv(args[0], args)


if __name__ == "__main__":
    main()
