import argparse
import datetime
import logging
import math
import sys
from pathlib import Path

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from option_dashboard_backend import OptionDashboardBackend
from option_dashboard_core import (
    HOLLOW_FACE_COLOR,
    LONG_POSITION_COLOR,
    SHORT_POSITION_COLOR,
    SIDE_SHORT,
    add_dashboard_common_args,
    add_web_server_args,
    bind_parser_error_handler,
    build_dashboard_header_data,
    build_server_settings,
    format_option_position_count_text,
    format_server_settings_text,
    get_dashboard_title,
    get_option_position_counts,
    get_profit_highlight_threshold,
    make_telegram_short_close_alert_handler,
    _fmt_int,
    _fmt_percent,
    _fmt_price,
    _get_option_quotes_batch,
    _get_stock_prices_with_fallback,
    _merge_option_quotes,
    _option_side,
    _options_hover_signature,
    _options_signature,
    _panel_key,
    _panel_title,
    _pick_price_option_code,
    _query_positions_with_log,
    _safe_float,
    _safe_int,
    get_options_map,
    get_options_delta_sum,
    get_options_short_value_sum,
    get_stock_share_delta_map,
    parse_ports_arg,
    safe_quote_ctx,
    safe_trade_ctx,
    set_profit_highlight_threshold,
)
from options import OptionEnum

logger = logging.getLogger("plot_positions_option_web")
LOG_FORMAT = "%(asctime)s - %(threadName)s - %(levelname)s - %(name)s - %(message)s"
logging.basicConfig(
    level=logging.INFO,
    format=LOG_FORMAT,
)

BASE_DIR = Path(__file__).resolve().parent
WEB_DIR = BASE_DIR / "web"

SHORT_COLOR = f"rgba({int(SHORT_POSITION_COLOR[0]*255)},{int(SHORT_POSITION_COLOR[1]*255)},{int(SHORT_POSITION_COLOR[2]*255)},{SHORT_POSITION_COLOR[3]})"
LONG_COLOR = f"rgba({int(LONG_POSITION_COLOR[0]*255)},{int(LONG_POSITION_COLOR[1]*255)},{int(LONG_POSITION_COLOR[2]*255)},{LONG_POSITION_COLOR[3]})"
HOLLOW_COLOR = f"rgba({int(HOLLOW_FACE_COLOR[0]*255)},{int(HOLLOW_FACE_COLOR[1]*255)},{int(HOLLOW_FACE_COLOR[2]*255)},{HOLLOW_FACE_COLOR[3]})"


def parse_args():
    parser = argparse.ArgumentParser(
        description="Web dashboard for option positions"
    )
    add_dashboard_common_args(
        parser, ui_help="frontend refresh interval seconds (default: 5)"
    )
    add_web_server_args(parser)
    bind_parser_error_handler(parser)
    args = parser.parse_args()

    stock_codes = [s.strip() for s in args.stock_codes.split(",") if s.strip()]
    if not stock_codes:
        parser.error("No valid stock codes provided. Example: US.AAPL,US.TSLA")

    ports = parse_ports_arg(args.port, parser, logger_obj=logger, max_ports=2)

    return {
        "stock_codes": stock_codes,
        "host": args.host,
        "ports": ports,
        "poll_interval": args.poll_interval,
        "price_interval": args.price_interval,
        "ui_interval": args.ui_interval,
        "price_mode": args.price_mode,
        "profit_highlight_threshold": args.profit_highlight_threshold,
        "telegram_bot_token": args.telegram_bot_token,
        "telegram_chat_id": args.telegram_chat_id,
        "web_host": args.web_host,
        "web_port": args.web_port,
    }


def _type_text(option_type):
    if option_type == OptionEnum.PUT:
        return "PUT"
    if option_type == OptionEnum.CALL:
        return "CALL"
    raw = str(option_type).upper()
    return "PUT" if "PUT" in raw else "CALL"


def _strike_date_to_iso(raw):
    try:
        return datetime.datetime.strptime(str(raw), "%y%m%d").strftime("%Y-%m-%d")
    except Exception:
        return str(raw)


def _marker_area(abs_count):
    # Match matplotlib scatter area (points^2) used in desktop script.
    return 40 + max(0, abs_count - 1) * 20


def _area_to_plotly_size(area):
    # Plotly marker.size is diameter in pixels, while matplotlib uses area.
    return max(4.0, 2.0 * math.sqrt(max(1.0, float(area)) / math.pi))


def _normalize_option(option):
    count = _safe_int(option.get("count"), 0)
    abs_count = abs(count)
    side = _option_side(option)
    option_type = _type_text(option.get("type"))
    strike_price = _safe_float(option.get("strike_price"), 0.0)
    strike_date_iso = _strike_date_to_iso(option.get("strike_date"))
    pl_ratio = _safe_float(option.get("pl_ratio"), 0.0)
    pl_val = _safe_float(option.get("pl_val"), None)
    pl_val_text = "N/A" if pl_val is None else f"{pl_val:+.2f}"
    profit_hit = pl_ratio >= get_profit_highlight_threshold()
    line_color = SHORT_COLOR if side == SIDE_SHORT else LONG_COLOR
    marker_area = _marker_area(abs_count)

    hover_text = (
        f"{strike_date_iso}, {strike_price:.2f}<br>"
        f"{side} {option_type}: count={abs_count}, "
        f"price={_fmt_price(option.get('price'))}<br>"
        f"bid={_fmt_price(option.get('bid_price'))}, "
        f"ask={_fmt_price(option.get('ask_price'))}, "
        f"volume={_fmt_int(option.get('volume'))}, "
        f"oi={_fmt_int(option.get('open_interest'))}<br>"
        f"profit%={_fmt_percent(option.get('pl_ratio'))}, "
        f"p/l={pl_val_text}"
    )

    return {
        "code": option.get("code"),
        "type": option_type,
        "side": side,
        "strike_date": strike_date_iso,
        "strike_price": strike_price,
        "count": count,
        "abs_count": abs_count,
        "pl_ratio": pl_ratio,
        "pl_val": pl_val,
        "profit_hit": profit_hit,
        "marker_symbol": "triangle-up" if option_type == "PUT" else "circle",
        "marker_area": marker_area,
        "marker_size": _area_to_plotly_size(marker_area),
        "marker_line_color": line_color,
        "marker_fill_color": line_color if profit_hit else HOLLOW_COLOR,
        "price": _safe_float(option.get("price"), None),
        "bid_price": _safe_float(option.get("bid_price"), None),
        "ask_price": _safe_float(option.get("ask_price"), None),
        "volume": _safe_int(option.get("volume"), None),
        "open_interest": _safe_int(option.get("open_interest"), None),
        "hover_text": hover_text,
    }


def build_uvicorn_log_config():
    return {
        "version": 1,
        "disable_existing_loggers": False,
        "formatters": {
            "default": {
                "format": LOG_FORMAT,
            },
        },
        "handlers": {
            "default": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stderr",
            },
            "access": {
                "class": "logging.StreamHandler",
                "formatter": "default",
                "stream": "ext://sys.stdout",
            },
        },
        "loggers": {
            "uvicorn": {
                "handlers": ["default"],
                "level": "INFO",
                "propagate": False,
            },
            "uvicorn.error": {
                "level": "INFO",
            },
            "uvicorn.access": {
                "handlers": ["access"],
                "level": "INFO",
                "propagate": False,
            },
        },
    }


def build_web_snapshot(backend, ui_interval, server_settings=None):
    state = backend.get_state_snapshot()
    prices_snapshot = state["prices"]
    options_snapshot = state["options"]
    delta_sum_by_panel = state.get("delta_sum_by_panel", {})
    options_done_at_by_port = state.get("options_done_at_by_port", {})
    price_done_at = state.get("price_done_at")
    options_version = state["options_version"]
    price_version = state["price_version"]

    panels = []
    for port_index, port in enumerate(backend.ports):
        for stock_code in backend.stock_codes:
            key = _panel_key(port_index, stock_code)
            raw_options = options_snapshot.get(key, [])
            options = [_normalize_option(option) for option in raw_options]
            delta_sum = _safe_float(delta_sum_by_panel.get(key), 0.0)
            short_value = get_options_short_value_sum(raw_options)
            position_counts = get_option_position_counts(raw_options)
            panels.append(
                {
                    "port_index": port_index,
                    "port": port,
                    "stock_code": stock_code,
                    "title": _panel_title(
                        stock_code,
                        port,
                        delta_sum=delta_sum,
                        short_value=short_value,
                    ),
                    "delta_sum": delta_sum,
                    "short_value": short_value,
                    "has_data": bool(options),
                    "stock_price": _safe_float(prices_snapshot.get(stock_code), None),
                    "position_count_text": format_option_position_count_text(
                        position_counts
                    ),
                    "options": options,
                }
            )

    generated_at = datetime.datetime.now(datetime.timezone.utc).isoformat()
    header = build_dashboard_header_data(
        generated_at=generated_at,
        ui_interval=ui_interval,
        options_version=options_version,
        price_version=price_version,
        price_done_at=price_done_at,
        ports=backend.ports,
        options_done_at_by_port=options_done_at_by_port,
    )

    return {
        "generated_at": generated_at,
        "header": header,
        "stock_codes": backend.stock_codes,
        "ports": backend.ports,
        "price_mode": backend.price_mode,
        "ui_interval": ui_interval,
        "profit_highlight_threshold": get_profit_highlight_threshold(),
        "versions": {
            "options": options_version,
            "price": price_version,
        },
        "options_done_at_by_port": options_done_at_by_port,
        "price_done_at": price_done_at,
        "server_settings": server_settings or {},
        "server_settings_text": format_server_settings_text(server_settings) if server_settings else "",
        "panels": panels,
    }


def create_app(backend, ui_interval, server_settings=None):
    app = FastAPI(title=get_dashboard_title())
    app.mount("/static", StaticFiles(directory=str(WEB_DIR)), name="static")

    @app.get("/", response_class=FileResponse)
    def index():
        return FileResponse(
            WEB_DIR / "index.html",
            headers={
                "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
                "Pragma": "no-cache",
                "Expires": "0",
            },
        )

    @app.get("/api/snapshot")
    def snapshot():
        return JSONResponse(build_web_snapshot(backend, ui_interval, server_settings))

    @app.get("/healthz")
    def healthz():
        return {"ok": True}

    return app


def main():
    args = parse_args()
    try:
        set_profit_highlight_threshold(args["profit_highlight_threshold"])
    except ValueError as e:
        logger.error("Invalid --profit_highlight_threshold: %s", e)
        sys.exit(1)
    short_alert_handler = make_telegram_short_close_alert_handler(
        args["telegram_bot_token"],
        args["telegram_chat_id"],
        logger_obj=logger,
    )
    if short_alert_handler:
        logger.info(
            "Short close alerts enabled on Telegram at threshold %.2f%%",
            args["profit_highlight_threshold"],
        )
    elif str(args["telegram_bot_token"]).strip() or str(args["telegram_chat_id"]).strip():
        logger.warning(
            "Telegram short close alerts disabled: both --telegram_bot_token and "
            "--telegram_chat_id are required"
        )
    server_settings = build_server_settings(
        stock_codes=args["stock_codes"],
        futu_host=args["host"],
        futu_ports=args["ports"],
        poll_interval=args["poll_interval"],
        price_interval=args["price_interval"],
        ui_interval=args["ui_interval"],
        price_mode=args["price_mode"],
        profit_highlight_threshold=args["profit_highlight_threshold"],
        telegram_alert_enabled=bool(short_alert_handler),
        web_host=args["web_host"],
        web_port=args["web_port"],
    )
    backend = OptionDashboardBackend(
        stock_codes=args["stock_codes"],
        host=args["host"],
        ports=args["ports"],
        poll_interval=args["poll_interval"],
        price_interval=args["price_interval"],
        price_mode=args["price_mode"],
        safe_trade_ctx=safe_trade_ctx,
        safe_quote_ctx=safe_quote_ctx,
        query_positions_with_log=_query_positions_with_log,
        get_options_map=get_options_map,
        get_option_quotes_batch=_get_option_quotes_batch,
        merge_option_quotes=_merge_option_quotes,
        get_stock_prices_with_fallback=_get_stock_prices_with_fallback,
        get_stock_share_delta_map=get_stock_share_delta_map,
        get_options_delta_sum=get_options_delta_sum,
        options_signature=_options_signature,
        options_hover_signature=_options_hover_signature,
        panel_key=_panel_key,
        pick_price_option_code=_pick_price_option_code,
        logger=logger,
        init_purpose_prefix="init_web",
        poll_purpose_prefix="poll_options_web",
        price_thread_name="poll_price_web",
        options_thread_name_prefix="poll_options_web_",
        short_alert_threshold=(
            args["profit_highlight_threshold"] if short_alert_handler else None
        ),
        short_alert_handler=short_alert_handler,
    )

    try:
        backend.start()
        app = create_app(backend, args["ui_interval"], server_settings=server_settings)
        logger.info(
            "Web server listening at http://%s:%s",
            args["web_host"],
            args["web_port"],
        )
        uvicorn.run(
            app,
            host=args["web_host"],
            port=args["web_port"],
            log_level="info",
            log_config=build_uvicorn_log_config(),
        )
    except Exception as e:
        logger.error("error in web dashboard: %s", e)
        sys.exit(1)
    finally:
        backend.stop()


if __name__ == "__main__":
    main()
