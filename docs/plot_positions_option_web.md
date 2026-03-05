# `plot_positions_option_web.py` Usage Guide

## Overview

`plot_positions_option_web.py` starts a local web server and renders option positions in browser charts.
The page assets are independent files under `web/` (`index.html`, `styles.css`, `app.js`), not inline strings in Python.

Display semantics are aligned with the Matplotlib version:
- X-axis: strike date
- Y-axis: strike price
- Marker shape: circle = call, triangle = put
- Marker color: green = short, pink = long
- Filled markers: `pl_ratio >= profit_highlight_threshold` (default `80%`)
- Red dashed horizontal line: underlying stock price baseline

If you provide two Futu ports, the dashboard shows one column per port for side-by-side comparison.

## Important Compatibility Note

This web script is a **new entrypoint** and does **not** replace the Matplotlib GUI entry.
Both entries now share backend modules, while GUI interaction flow remains unchanged.

## Prerequisites

1. Python 3
2. Installed packages from `requirements.txt`
3. Futu OpenD running and logged in
4. Account permissions to query option positions/quotes

## Command Syntax

```bash
python plot_positions_option_web.py <stock_codes> [--host HOST] [--port PORTS] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC] [--price_mode MODE] [--profit_highlight_threshold PCT] [--telegram_bot_token TOKEN] [--telegram_chat_id CHAT_ID] [--web_host HOST] [--web_port PORT]
```

## Arguments

- `stock_codes` (required): comma-separated stock codes, e.g. `US.AAPL,US.TSLA`
- `--host`: Futu host, default `127.0.0.1`
- `--port`: one or two Futu ports, default `11111`
- `--poll_interval`: option polling interval seconds, default `10`
- `--price_interval`: price polling interval seconds, default `10`
- `--ui_interval`: browser refresh interval seconds, default `5`
- `--price_mode`: `auto|last|pre|after|overnight|implied`, default `implied`
- `--profit_highlight_threshold`: filled marker threshold percent, default `80`
- `--telegram_bot_token`: Telegram bot token, default env `TELEGRAM_BOT_TOKEN`
- `--telegram_chat_id`: Telegram chat id for close-alert message, default env `TELEGRAM_CHAT_ID`
- `--web_host`: web server host, default `127.0.0.1`
- `--web_port`: web server port, default `18080`

## Examples

Single stock, single port:

```bash
python plot_positions_option_web.py US.AAPL
```

Multiple stocks:

```bash
python plot_positions_option_web.py "US.AAPL,US.TSLA,US.NVDA" --port 11111
```

Two ports side-by-side:

```bash
python plot_positions_option_web.py "US.AAPL,US.TSLA" --port 11111,11112 --poll_interval 8 --price_interval 3 --ui_interval 2
```

Use 70% as filled-marker threshold:

```bash
python plot_positions_option_web.py US.AAPL --profit_highlight_threshold 70
```

Enable Telegram close alerts for short options:

```bash
python plot_positions_option_web.py US.AAPL \
  --telegram_bot_token <BOT_TOKEN> \
  --telegram_chat_id <CHAT_ID>
```

Open the dashboard:

```text
http://127.0.0.1:18080
```

## Runtime Behavior

- Shares backend modules (`option_dashboard_backend.py` + `option_dashboard_core.py`) with the Matplotlib GUI entry
- One options polling thread per Futu port
- One shared stock-price polling thread
- Telegram short-close alerts are emitted only when a short option newly crosses the threshold (deduplicated per option code)
- HTTP API endpoint `/api/snapshot` for frontend updates
- Health endpoint `/healthz`

## Troubleshooting

- `ModuleNotFoundError: uvicorn` or `fastapi`: run `pip install -r requirements.txt`
- Empty charts: verify OpenD login, account permissions, and selected stock codes
- Connection errors: verify `--host` and `--port`
