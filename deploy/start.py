#!/usr/bin/python3
"""Root-installed launcher; all repository code runs as the opdash user."""
import os
from pathlib import Path


# Bootstrap in the release venv so retained releases also support encryption.
# Their CLI may predate --rsa_private_key; configure the SDK before importing them.
ENCRYPTED_BOOTSTRAP = """
import os, runpy, sys
from futu import SysConfig
SysConfig.set_init_rsa_file(os.environ['FUTU_RSA_PRIVATE_KEY'])
SysConfig.enable_proto_encrypt(True)
script = sys.argv.pop(1)
sys.argv[0] = script
sys.path.insert(0, os.path.dirname(script))
runpy.run_path(script, run_name='__main__')
"""


def main():
    # The Web app owns background connection/retry so HTTP can report OpenD errors.
    release = Path("/opt/opdash/current").resolve(strict=True)
    os.environ["OPDASH_RELEASE"] = release.name
    args = [str(release / ".venv/bin/python"), "-u", str(release / "opdash_web.py")]
    if os.environ.get("FUTU_RSA_PRIVATE_KEY"):
        args[2:2] = ["-c", ENCRYPTED_BOOTSTRAP]
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
