# `opdash.py` Usage Guide

For the web dashboard entry, see [`docs/plot_positions_option_web.md`](plot_positions_option_web.md).

## Data Source Support

- Currently only supports `futu-api`: https://openapi.futunn.com/futu-api-doc/en/intro/intro.html

## Overview

`opdash.py` opens a live Matplotlib window to visualize your **option positions**.

For each stock:
- X-axis: strike date
- Y-axis: strike price
- Marker shape:
  - circle = call option
  - triangle = put option
- Marker color:
  - green = short position
  - pink = long position
- Marker size grows with contract count
- Filled markers mean `pl_ratio >= profit_highlight_threshold` (default `80%`)
- A single page-level legend (top-right corner) explains shape/color semantics
- A red dashed horizontal line shows the underlying stock price

If you pass two ports, charts are shown side by side (one column per port) for comparison.

## Prerequisites

1. Python 3.
2. Installed Python packages:
   - `futu-api`
   - `matplotlib`
   - `mplcursors`
   - `numpy`
3. Futu OpenD (or compatible Futu gateway) running and reachable.
4. A trading account that can query US option positions.

OpenD must be started and logged in before running this script; otherwise it cannot fetch position/quote data.
If you use two ports (for example `11111,11112`), start two OpenD instances and assign different listening ports to each instance.

Official OpenD startup docs:
- Visual OpenD: https://openapi.futunn.com/futu-api-doc/quick/opend-base.html
- Command Line OpenD: https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html

Example install command:

```bash
pip install futu-api matplotlib mplcursors numpy
```

## Command Syntax

```bash
python opdash.py <stock_codes> [--host HOST] [--port PORTS] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC] [--price_mode MODE] [--profit_highlight_threshold PCT] [--telegram_bot_token TOKEN] [--telegram_chat_id CHAT_ID]
```

### Required argument

- `stock_codes`: Comma-separated stock codes, for example:
  - `US.AAPL`
  - `US.AAPL,US.TSLA`
  - `"US.AAPL, US.TSLA"` (spaces are allowed)

### Optional arguments

- `--host`  
  Futu server host. Default: `127.0.0.1`

- `--port`  
  One or two Futu ports, comma-separated. Default: `11111`  
  Examples: `11111` or `11111,11112`  
  Note: If more than two ports are provided, only the first two are used.

- `--poll_interval`  
  Option position polling interval (seconds). Default: `10`

- `--price_interval`  
  Price polling interval for the red baseline (seconds). Default: `10`

- `--ui_interval`  
  UI refresh interval (seconds). Default: `5`

- `--price_mode`
  Price source mode for red baseline. Default: `implied`
  - `auto`: choose from `pre_price/last_price/after_price/overnight_price` by US market state, then fallback to implied-from-option when unavailable
  - `last`: use regular last price, then fallback to implied
  - `pre`: prefer pre-market price, then fallback
  - `after`: prefer after-hours price, then fallback
  - `overnight`: prefer overnight price, then fallback
  - `implied`: only use implied-from-option (legacy behavior)

- `--profit_highlight_threshold`
  Filled marker threshold percent. Default: `80`

- `--telegram_bot_token`
  Telegram bot token. Default: env `TELEGRAM_BOT_TOKEN`

- `--telegram_chat_id`
  Telegram chat id used for close-alert messages. Default: env `TELEGRAM_CHAT_ID`

## Quick Start Examples

Single stock, single port:

```bash
python opdash.py US.AAPL
```

Multiple stocks, single port:

```bash
python opdash.py "US.AAPL,US.TSLA,US.NVDA" --port 11111
```

Compare two ports side by side:

```bash
python opdash.py "US.AAPL,US.TSLA" --host 127.0.0.1 --port 11111,11112 --poll_interval 8 --price_interval 3 --ui_interval 1
```

Use default mode (`implied`) for best compatibility:

```bash
python opdash.py US.AAPL
```

Prefer pre-market / after-hours price when available:

```bash
python opdash.py US.AAPL --price_mode auto
```

Set filled-marker threshold to 70%:

```bash
python opdash.py US.AAPL --profit_highlight_threshold 70
```

Enable Telegram close alerts for short options:

```bash
python opdash.py US.AAPL \
  --telegram_bot_token <BOT_TOKEN> \
  --telegram_chat_id <CHAT_ID>
```

Show built-in help:

```bash
python opdash.py -h
```

## Runtime Behavior

- The script reads non-zero option positions (`qty != 0`) from holdings.
- It creates one row per stock code and one column per port.
- It starts background polling threads:
  - one thread per port for option-position updates
  - one shared thread for price updates
- Close the chart window to stop polling and exit cleanly.

## Common Issues

- `No valid stock codes provided`: check your `stock_codes` input format.
- `No option positions for <code>`: there are no option positions for that stock on that port.
- Connection or timeout errors: verify `--host`, `--port`, and OpenD status.
- No window shown in remote/headless shell: run in an environment with a GUI backend for Matplotlib.

## Notes

- Trading context is created for the US market in current code.
- Profit highlight threshold defaults to `80.0` and can be changed with `--profit_highlight_threshold`.
- If your Futu quote permission cannot provide extended-session fields, the script automatically falls back to implied-from-option price.
