# opdash Detailed Usage

## Scope

This document is the detailed reference for the entrypoints:

- `opdash.py`: Matplotlib GUI dashboard
- `opdash_web.py`: browser-based web dashboard
- `close_short_calls.py`: places buy-to-close orders for one underlying's short calls

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

On the deployed Osaka LAN (`192.168.10.0/24`), OpenD requires encrypted SDK connections:

```bash
python opdash.py --host 192.168.10.1 --port 11111 --rsa_private_key ~/.config/futu/futu-opend-rsa.pem
python opdash_web.py --host 192.168.10.1 --port 11111 --rsa_private_key ~/.config/futu/futu-opend-rsa.pem
```

The private key must match the gateway's `rsa_private_key` configuration. Both entrypoints
also accept the `FUTU_RSA_PRIVATE_KEY` environment variable; an explicit CLI path takes
precedence. Configure encryption before creating any quote or trade context. The gateway
dashboard continues to use `127.0.0.1:11111`, with encryption enabled by its launcher.
See [gateway deployment](deployment.md) for configuration and verification details.

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
- Orange (Web): a short position that is in the money — the stock price is above a short call's strike or below a short put's strike. The marker turns solid orange, the panel gets an orange border and a `short ITM: N` count in its title, and the hover text shows how far in the money it is. Long positions are not flagged
- Red dashed line: underlying stock price

If you pass two ports, charts are shown side by side.

### Syntax

```bash
python opdash.py [stock_codes] [--host HOST] [--port PORTS] [--rsa_private_key FILE] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC] [--price_mode MODE] [--profit_highlight_threshold PCT]
```

### Arguments

- `stock_codes`: optional, comma-separated stock codes such as `US.AAPL`, `HK.00700`, `HK.TCH`. Omit it to show every option held in the account: the underlyings are discovered from the option positions (union across ports) and re-discovered on every poll, so panels are added and removed at runtime as positions open and close — no restart needed. Passing codes explicitly keeps the panel set fixed
- `--host`: Futu host, default `127.0.0.1`
- `--port`: one or two Futu ports, default `11111`
- `--rsa_private_key`: shared RSA private-key file; enables SDK encryption. Defaults to `FUTU_RSA_PRIVATE_KEY` if set. Both ports must use the same key when comparing two OpenD instances
- `--poll_interval`: option polling interval seconds, default `10`
- `--price_interval`: price polling interval seconds, default `10`
- `--ui_interval`: UI refresh interval seconds, default `5`
- `--price_mode`: `auto|last|pre|after|overnight|implied`, default `auto`. HK option class codes such as `HK.TCH` are priced from the underlying reported by the option quote (`HK.00700`), and fall back to `implied` until that quote arrives
- `--profit_highlight_threshold`: filled-marker threshold percent, default `80`

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
python opdash_web.py [stock_codes] [--host HOST] [--port PORTS] [--poll_interval SEC] [--price_interval SEC] [--ui_interval SEC] [--price_mode MODE] [--profit_highlight_threshold PCT] [--web_host HOST] [--web_port PORT]
```

Web auto-discovery also starts with an empty account and keeps polling for new positions.
`/healthz` checks HTTP liveness and the deployed release; `/readyz` returns 503 when
position polls or per-underlying price retrieval become stale, or as soon as a poll
fails outright. A rejected query that leaves positions and prices refreshing keeps
`/readyz` at 200. See [gateway deployment](deployment.md).

The page starts before OpenD is ready. A banner above the charts shows connection,
encryption, position-query, and quote-query failures with the endpoint, error, time,
and suggested fix. Existing charts retain the last successful data and are marked
as potentially stale. When data is still refreshing despite a rejected query, the
banner says so instead and lists what failed. Errors clear after the affected operation succeeds; another
port's errors remain visible. Browser refresh failures are also shown and retried.

### Arguments

- `stock_codes`: optional, comma-separated stock codes such as `US.AAPL`, `HK.00700`, `HK.TCH`. Omit it to show every option held in the account: the underlyings are discovered from the option positions (union across ports) and re-discovered on every poll, so panels are added and removed at runtime as positions open and close — no restart needed. Passing codes explicitly keeps the panel set fixed
- `--host`: Futu host, default `127.0.0.1`
- `--port`: one or two Futu ports, default `11111`
- `--rsa_private_key`: shared RSA private-key file; enables SDK encryption. Defaults to `FUTU_RSA_PRIVATE_KEY` if set. Both ports must use the same key when comparing two OpenD instances
- `--poll_interval`: option polling interval seconds, default `10`
- `--price_interval`: price polling interval seconds, default `10`
- `--ui_interval`: browser refresh interval seconds, default `5`
- `--price_mode`: `auto|last|pre|after|overnight|implied`, default `auto`. HK option class codes such as `HK.TCH` are priced from the underlying reported by the option quote (`HK.00700`), and fall back to `implied` until that quote arrives
- `--profit_highlight_threshold`: filled marker threshold percent, default `80`
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

Then open:

```text
http://127.0.0.1:18080
```

### Runtime Behavior

- Exposes `/api/snapshot` for frontend polling
- Exposes `/healthz`
- Uses the same backend polling model as the GUI entrypoint

## `close_short_calls.py`

### Overview

`close_short_calls.py` buys back every short call on one underlying with a day limit order at
the current bid. It places real orders on the real account, so try `--dry_run` first.

- Contracts already on working buy orders are not ordered again. Working orders come from
  today's order list plus a year of history, so GTC (up to 90 days) and GTD orders from
  earlier days count, and so do disabled HK orders, which can be re-enabled. Partially
  filled orders count only their unfilled quantity, and run the script again any time to
  top up only what is missing. Orders are read before positions, so a close order that
  fills mid-run leads to ordering too few, never a duplicate
- Calls with no bid (for example, outside market hours) are skipped
- Raise prices yourself in the Futu app; the script does not chase the market

### Syntax

```bash
python close_short_calls.py stock_code [--host HOST] [--port PORT] [--rsa_private_key FILE] [--acc_id ID] [--unlock] [--dry_run]
```

### Arguments

- `stock_code`: required underlying as it appears in option codes, such as `US.AAPL` or `HK.TCH`; a code without a market prefix is treated as US (`AAPL` -> `US.AAPL`). HK numeric stock codes such as `HK.00700` are rejected: pass the option class code (`HK.TCH`)
- `--host`: Futu host, default `127.0.0.1`
- `--port`: one Futu port, default `11111`
- `--rsa_private_key`: shared RSA private-key file; defaults to `FUTU_RSA_PRIVATE_KEY` if set
- `--acc_id`: real trading account id; needed only when OpenD has more than one active real account. The account is printed masked
- `--unlock`: prompt for the trade password and unlock trading on OpenD first. The password is read without echo and is never stored
- `--dry_run`: print the orders without placing them

### Examples

```bash
python close_short_calls.py US.AAPL --dry_run
python close_short_calls.py AAPL --unlock
python close_short_calls.py HK.TCH --host 192.168.10.1 --rsa_private_key ~/.config/futu/futu-opend-rsa.pem
```

The exit status is non-zero when a short call is left without a close order (no bid, or the
order is rejected or cancelled within a few seconds of placing it), when OpenD cannot be
reached, or when a query fails before ordering. A connection failure, such as a missing
`--rsa_private_key` on an encrypted OpenD, stops the script with a hint instead of retrying.

## Troubleshooting

- `No valid stock codes provided`: verify the input format
- `No option positions for <code>`: that stock has no option positions on that port
- `ModuleNotFoundError` for `uvicorn` or `fastapi`: activate `.venv`, then run `pip install -r requirements.txt`
- Connection or timeout errors: verify `--host`, `--port`, and OpenD status
- No GUI window in a remote shell: use an environment with a Matplotlib GUI backend
- `Trade is not unlocked`: run `close_short_calls.py` once with `--unlock`
- `Wrong trading password`: check the input method and that OpenD is logged in to the Futu ID this trade password belongs to; repeated failures lock the trade password
