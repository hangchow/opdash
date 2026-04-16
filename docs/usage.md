# opdash Detailed Usage

## Scope

This document is the detailed reference for both entrypoints:

- `opdash.py`: Matplotlib GUI dashboard
- `opdash_web.py`: browser-based web dashboard

For project overview and shortest-start examples, see the root [README](../README.md).

## Prerequisites

1. Python 3
2. Futu OpenD running and logged in
3. Account permissions to query positions and quotes

Environment setup:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

All commands below assume the virtual environment is already activated.

## Common Notes

- Data source: `futu-api`
- If you compare two ports such as `11111,11112`, run two OpenD instances and assign different listening ports.
- Supported stock-code inputs include `US.AAPL`, `HK.00700`, and `HK.TCH`.
- GUI and Web share the same backend logic in `backend.py` and `core.py`.
- Telegram short-close alerts trigger only when a short option newly crosses `--profit_highlight_threshold`.

Official OpenD startup docs:

- Visual OpenD: https://openapi.futunn.com/futu-api-doc/quick/opend-base.html
- Command Line OpenD: https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html

## `opdash.py`

### Overview

`opdash.py` opens a live Matplotlib window to visualize option positions.

- X-axis: strike date
- Y-axis: strike price
- Circle: call option
- Triangle: put option
- Green: short position
- Pink: long position
- Filled markers: `pl_ratio >= profit_highlight_threshold`
- Red dashed line: underlying stock price

If you pass two ports, charts are shown side by side.

### Syntax

```bash
python opdash.py <stock_codes> [--host HOST] [--port PORTS] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC] [--price_mode MODE] [--profit_highlight_threshold PCT] [--telegram_bot_token TOKEN] [--telegram_chat_id CHAT_ID]
```

### Arguments

- `stock_codes`: comma-separated stock codes such as `US.AAPL`, `HK.00700`, `HK.TCH`
- `--host`: Futu host, default `127.0.0.1`
- `--port`: one or two Futu ports, default `11111`
- `--poll_interval`: option polling interval seconds, default `10`
- `--price_interval`: price polling interval seconds, default `10`
- `--ui_interval`: UI refresh interval seconds, default `5`
- `--price_mode`: `auto|last|pre|after|overnight|implied`, default `implied`
- `--profit_highlight_threshold`: filled-marker threshold percent, default `80`
- `--telegram_bot_token`: Telegram bot token, default env `TELEGRAM_BOT_TOKEN`
- `--telegram_chat_id`: Telegram chat id, default env `TELEGRAM_CHAT_ID`

### Examples

Single stock:

```bash
python opdash.py US.AAPL
```

Two ports:

```bash
python opdash.py "US.AAPL,US.TSLA" --port 11111,11112
```

Hong Kong stock by numeric code:

```bash
python opdash.py HK.00700 --port 11111,22222 --profit_highlight_threshold 70
```

Hong Kong stock by alias:

```bash
python opdash.py HK.TCH --port 11111,22222 --profit_highlight_threshold 70
```

Telegram alerts:

```bash
python opdash.py US.AAPL \
  --telegram_bot_token <BOT_TOKEN> \
  --telegram_chat_id <CHAT_ID>
```

Built-in help:

```bash
python opdash.py -h
```

### Runtime Behavior

- Reads non-zero positions from holdings
- Creates one row per stock and one column per port
- Starts one option-polling thread per port
- Starts one shared price-polling thread
- Stops cleanly when the chart window closes

## `opdash_web.py`

### Overview

`opdash_web.py` starts a local web server and renders option positions in browser charts.

- Display semantics match the Matplotlib version
- The frontend assets live under `web/`
- If you pass two ports, panels are shown side by side

### Syntax

```bash
python opdash_web.py <stock_codes> [--host HOST] [--port PORTS] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC] [--price_mode MODE] [--profit_highlight_threshold PCT] [--telegram_bot_token TOKEN] [--telegram_chat_id CHAT_ID] [--web_host HOST] [--web_port PORT]
```

### Arguments

- `stock_codes`: comma-separated stock codes such as `US.AAPL`, `HK.00700`, `HK.TCH`
- `--host`: Futu host, default `127.0.0.1`
- `--port`: one or two Futu ports, default `11111`
- `--poll_interval`: option polling interval seconds, default `10`
- `--price_interval`: price polling interval seconds, default `10`
- `--ui_interval`: browser refresh interval seconds, default `5`
- `--price_mode`: `auto|last|pre|after|overnight|implied`, default `implied`
- `--profit_highlight_threshold`: filled marker threshold percent, default `80`
- `--telegram_bot_token`: Telegram bot token, default env `TELEGRAM_BOT_TOKEN`
- `--telegram_chat_id`: Telegram chat id, default env `TELEGRAM_CHAT_ID`
- `--web_host`: web server host, default `127.0.0.1`
- `--web_port`: web server port, default `18080`

### Examples

Single stock:

```bash
python opdash_web.py US.AAPL
```

Multiple stocks:

```bash
python opdash_web.py "US.AAPL,US.TSLA,US.NVDA" --port 11111
```

Two ports:

```bash
python opdash_web.py "US.AAPL,US.TSLA" --port 11111,11112 --poll_interval 8 --price_interval 3 --ui_interval 2
```

Hong Kong stock:

```bash
python opdash_web.py HK.00700 --port 11111,22222 --profit_highlight_threshold 70
```

Telegram alerts:

```bash
python opdash_web.py US.AAPL \
  --telegram_bot_token <BOT_TOKEN> \
  --telegram_chat_id <CHAT_ID>
```

Then open:

```text
http://127.0.0.1:18080
```

### Runtime Behavior

- Exposes `/api/snapshot` for frontend polling
- Exposes `/healthz`
- Uses the same backend polling model as the GUI entrypoint

## Troubleshooting

- `No valid stock codes provided`: verify the input format
- `No option positions for <code>`: that stock has no option positions on that port
- `ModuleNotFoundError` for `uvicorn` or `fastapi`: activate `.venv`, then run `pip install -r requirements.txt`
- Connection or timeout errors: verify `--host`, `--port`, and OpenD status
- No GUI window in a remote shell: use an environment with a Matplotlib GUI backend
