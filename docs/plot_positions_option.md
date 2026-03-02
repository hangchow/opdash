# `plot_positions_option.py` Usage Guide

## Overview

`plot_positions_option.py` opens a live Matplotlib window to visualize your **option positions**.

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
- Filled markers mean `pl_ratio >= 80%`
- A single in-chart legend (upper-left panel) explains shape/color semantics
- A red dashed horizontal line shows the inferred underlying stock price

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

Example install command:

```bash
pip install futu-api matplotlib mplcursors numpy
```

## Command Syntax

```bash
python plot_positions_option.py <stock_codes> [--host HOST] [--port PORTS] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC]
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

## Quick Start Examples

Single stock, single port:

```bash
python plot_positions_option.py US.AAPL
```

Multiple stocks, single port:

```bash
python plot_positions_option.py "US.AAPL,US.TSLA,US.NVDA" --port 11111
```

Compare two ports side by side:

```bash
python plot_positions_option.py "US.AAPL,US.TSLA" --host 127.0.0.1 --port 11111,11112 --poll_interval 8 --price_interval 3 --ui_interval 1
```

Show built-in help:

```bash
python plot_positions_option.py -h
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
- Profit highlight threshold is fixed at `80.0` in the script.
