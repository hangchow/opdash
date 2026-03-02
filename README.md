# opdash

Options position dashboard and plotting tool.

## Contents

- `plot_positions_option.py`: main visualization script
- `options.py`, `positions.py`, `stocks.py`: required local modules
- [`docs/plot_positions_option.md`](docs/plot_positions_option.md): usage guide

## Data Source Support

- Currently only supports `futu-api`: https://openapi.futunn.com/futu-api-doc/en/intro/intro.html

## Quick Start

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
python plot_positions_option.py -h
```

Default price baseline mode is `implied` (works without paid US stock quote permission):

```bash
python plot_positions_option.py US.AAPL
```

If you have extended-hours quote permission, you can switch to:

```bash
python plot_positions_option.py US.AAPL --price_mode auto
```
