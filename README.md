# opdash

Options position dashboard and plotting tool.

## Contents

- `plot_positions_option.py`: Matplotlib GUI visualization script (existing)
- `plot_positions_option_web.py`: web dashboard entry
- `option_dashboard_backend.py`: shared polling backend for GUI/Web
- `web/index.html`, `web/styles.css`, `web/app.js`: standalone web page assets
- `options.py`, `positions.py`, `stocks.py`: required local modules
- [`docs/plot_positions_option.md`](docs/plot_positions_option.md): Matplotlib GUI usage guide
- [`docs/plot_positions_option_web.md`](docs/plot_positions_option_web.md): web dashboard usage guide

## Data Source Support

- Currently only supports `futu-api`: https://openapi.futunn.com/futu-api-doc/en/intro/intro.html
- You must start and log in to Futu OpenD before running, otherwise the program cannot request quote/position data.
- If you want to compare two ports (for example `11111,11112`), run two OpenD instances and make each instance listen on a different port.
- Official OpenD startup docs:
  - Visual OpenD: https://openapi.futunn.com/futu-api-doc/quick/opend-base.html
  - Command Line OpenD: https://openapi.futunn.com/futu-api-doc/opend/opend-cmd.html

## Quick Start

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

Matplotlib GUI:

```bash
python plot_positions_option.py US.AAPL
```

Web dashboard:

```bash
python plot_positions_option_web.py US.AAPL --web_host 127.0.0.1 --web_port 18080
```

Then open `http://127.0.0.1:18080` in your browser.

## Notes

- GUI and Web now share `option_dashboard_backend.py` + `option_dashboard_core.py` for backend logic.
- Existing GUI behavior and the thread model stay the same (one options thread per port + one shared price thread).
