import logging
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from contextlib import contextmanager

import numpy as np
from futu import (RET_OK, OpenQuoteContext, OpenSecTradeContext, OptionType,
                  SecurityFirm, TrdMarket)

from options import OptionEnum
from positions import query_hold_positions

logger = logging.getLogger("plot_positions_option")

DEFAULT_PROFIT_HIGHLIGHT_THRESHOLD = 80.0
PROFIT_HIGHLIGHT_THRESHOLD = DEFAULT_PROFIT_HIGHLIGHT_THRESHOLD  # Highlight threshold
DEFAULT_TELEGRAM_TIMEOUT_SECONDS = 8.0
SHORT_POSITION_COLOR = (0.0, 0.6, 0.0, 1.0)    # Green: short
LONG_POSITION_COLOR = (1.0, 0.41, 0.71, 1.0)   # Pink: long
HOLLOW_FACE_COLOR = (0.0, 0.0, 0.0, 0.0)       # Hollow marker fill
SIDE_SHORT = "SHORT"
SIDE_LONG = "LONG"
_last_profit_hit_codes = {}
US_PRE_MARKET_STATES = {"PRE_MARKET_BEGIN"}
US_REGULAR_MARKET_STATES = {"AFTERNOON"}
US_AFTER_HOURS_STATES = {"AFTER_HOURS_BEGIN"}
US_OVERNIGHT_STATES = {"OVERNIGHT", "AFTER_HOURS_END"}
DASHBOARD_TITLE = "Option Positions Dashboard"


def bind_parser_error_handler(parser):
    # 覆写 argparse error 方法，输出错误信息后退出
    def custom_error(message):
        sys.stderr.write(f"Error: {message}\n\n")
        parser.print_help()
        sys.exit(1)

    parser.error = custom_error


def add_dashboard_common_args(parser, *, ui_help="ui refresh interval seconds (default: 5)"):
    parser.add_argument(
        "stock_codes",
        help="stock codes (e.g., US.UVXY) that options belong to",
    )
    parser.add_argument(
        "--host",
        metavar="",
        default="127.0.0.1",
        help="futu server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--port",
        metavar="",
        default="11111",
        help="futu server port(s), comma separated, max 2 (default: 11111)",
    )
    parser.add_argument(
        "--poll_interval",
        metavar="",
        type=int,
        default=10,
        help="poll options interval seconds (default: 10)",
    )
    parser.add_argument(
        "--price_interval",
        metavar="",
        type=int,
        default=10,
        help="poll price interval seconds (default: 10)",
    )
    parser.add_argument(
        "--ui_interval",
        metavar="",
        type=int,
        default=5,
        help=ui_help,
    )
    parser.add_argument(
        "--price_mode",
        metavar="",
        choices=["auto", "last", "pre", "after", "overnight", "implied"],
        default="implied",
        help=(
            "price source mode: auto/last/pre/after/overnight/implied "
            "(default: implied)"
        ),
    )
    parser.add_argument(
        "--profit_highlight_threshold",
        metavar="",
        type=float,
        default=DEFAULT_PROFIT_HIGHLIGHT_THRESHOLD,
        help=(
            "filled marker threshold in percent; "
            f"default: {DEFAULT_PROFIT_HIGHLIGHT_THRESHOLD:g}"
        ),
    )
    parser.add_argument(
        "--telegram_bot_token",
        metavar="",
        default=os.getenv("TELEGRAM_BOT_TOKEN", ""),
        help="telegram bot token (default: env TELEGRAM_BOT_TOKEN)",
    )
    parser.add_argument(
        "--telegram_chat_id",
        metavar="",
        default=os.getenv("TELEGRAM_CHAT_ID", ""),
        help="telegram chat id (default: env TELEGRAM_CHAT_ID)",
    )


def set_profit_highlight_threshold(value):
    global PROFIT_HIGHLIGHT_THRESHOLD
    threshold = float(value)
    if not np.isfinite(threshold):
        raise ValueError("profit_highlight_threshold must be finite")
    PROFIT_HIGHLIGHT_THRESHOLD = threshold


def get_profit_highlight_threshold():
    return PROFIT_HIGHLIGHT_THRESHOLD


def add_web_server_args(parser):
    parser.add_argument(
        "--web_host",
        metavar="",
        default="127.0.0.1",
        help="web server host (default: 127.0.0.1)",
    )
    parser.add_argument(
        "--web_port",
        metavar="",
        type=int,
        default=18080,
        help="web server port (default: 18080)",
    )


def build_server_settings(
    *,
    stock_codes,
    futu_host,
    futu_ports,
    poll_interval,
    price_interval,
    ui_interval,
    price_mode,
    profit_highlight_threshold,
    telegram_alert_enabled=False,
    web_host=None,
    web_port=None,
    started_at=None,
):
    return {
        "started_at": started_at or datetime.now(timezone.utc).isoformat(),
        "stock_codes": list(stock_codes or []),
        "futu_host": futu_host,
        "futu_ports": list(futu_ports or []),
        "poll_interval": poll_interval,
        "price_interval": price_interval,
        "ui_interval": ui_interval,
        "price_mode": price_mode,
        "profit_highlight_threshold": profit_highlight_threshold,
        "telegram_alert_enabled": bool(telegram_alert_enabled),
        "web_host": web_host,
        "web_port": web_port,
    }


def format_server_settings_text(server_settings, prefix="server settings"):
    s = server_settings or {}
    stock_codes = s.get("stock_codes") or []
    futu_ports = s.get("futu_ports") or []
    stock_codes_text = ",".join(str(code) for code in stock_codes) if stock_codes else "-"
    futu_ports_text = ",".join(str(port) for port in futu_ports) if futu_ports else "-"
    raw_started_at = s.get("started_at")
    started_at_text = _format_display_datetime(raw_started_at)
    if started_at_text == "-" and raw_started_at:
        started_at_text = str(raw_started_at)
    threshold = s.get("profit_highlight_threshold")
    if threshold is None:
        threshold_text = "-"
    else:
        try:
            threshold_text = f"{float(threshold):g}"
        except (TypeError, ValueError):
            threshold_text = str(threshold)
    parts = [
        f"{prefix}: started_at={started_at_text}",
        f"stock_codes={stock_codes_text}",
        f"futu_host={s.get('futu_host') or '-'} futu_ports={futu_ports_text}",
        (
            f"poll_interval={s.get('poll_interval', '-')}s "
            f"price_interval={s.get('price_interval', '-')}s "
            f"ui_interval={s.get('ui_interval', '-')}s"
        ),
        f"price_mode={s.get('price_mode') or '-'}",
        f"profit_highlight_threshold={threshold_text}",
        f"short_close_alert_threshold={threshold_text}",
        f"telegram_alert={'on' if s.get('telegram_alert_enabled') else 'off'}",
    ]
    if s.get("web_host") is not None or s.get("web_port") is not None:
        parts.append(f"web={s.get('web_host') or '-'}:{s.get('web_port') or '-'}")
    return " | ".join(parts)


def get_dashboard_title():
    return DASHBOARD_TITLE


def _coerce_datetime(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        dt = value
    else:
        raw = str(value).strip()
        if not raw:
            return None
        if raw.endswith("Z"):
            raw = f"{raw[:-1]}+00:00"
        try:
            dt = datetime.fromisoformat(raw)
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _format_display_datetime(value):
    dt = _coerce_datetime(value)
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _format_display_time(value):
    dt = _coerce_datetime(value)
    if dt is None:
        return "-"
    return dt.astimezone().strftime("%H:%M:%S")


def format_options_done_text(ports, options_done_at_by_port):
    ports_list = list(ports or [])
    if not ports_list:
        return "-"
    done_map = options_done_at_by_port or {}
    parts = []
    for port in ports_list:
        done_value = done_map.get(str(port))
        if done_value is None:
            done_value = done_map.get(port)
        parts.append(f"{port}:{_format_display_time(done_value)}")
    return ", ".join(parts)


def format_dashboard_status_text(
    *,
    generated_at,
    ui_interval,
    options_version,
    price_version,
    price_done_at=None,
    ports,
    options_done_at_by_port,
):
    generated_at_text = _format_display_datetime(generated_at)
    price_done_text = _format_display_time(price_done_at)
    options_done_text = format_options_done_text(ports, options_done_at_by_port)
    return (
        f"updated: {generated_at_text} | "
        f"options_loaded={options_done_text} | "
        f"price_loaded={price_done_text}"
    )


def build_dashboard_header_data(
    *,
    ui_interval,
    options_version,
    price_version,
    price_done_at=None,
    ports,
    options_done_at_by_port,
    generated_at=None,
    title=None,
):
    generated_at_iso = generated_at or datetime.now(timezone.utc).isoformat()
    header_title = title or get_dashboard_title()
    status_text = format_dashboard_status_text(
        generated_at=generated_at_iso,
        ui_interval=ui_interval,
        options_version=options_version,
        price_version=price_version,
        price_done_at=price_done_at,
        ports=ports,
        options_done_at_by_port=options_done_at_by_port,
    )
    return {
        "title": header_title,
        "status_text": status_text,
        "generated_at": generated_at_iso,
    }


def parse_ports_arg(raw_port, parser, logger_obj=None, max_ports=2):
    ports = []
    for raw in str(raw_port).split(","):
        raw = raw.strip()
        if not raw:
            continue
        try:
            ports.append(int(raw))
        except ValueError:
            parser.error(f"--port contains non-integer value: {raw}")
    if not ports:
        parser.error("--port must provide at least one valid integer port")
    if len(ports) > max_ports:
        if logger_obj is not None:
            logger_obj.warning(
                "--port provided %s values, only first %s will be used: %s",
                len(ports),
                max_ports,
                ports[:max_ports],
            )
        ports = ports[:max_ports]
    return ports


def _safe_float(value, default=0.0):
    # 行情/持仓字段可能出现 None、"--"、NaN/Inf，这里统一做容错转换
    try:
        num = float(value)
    except (TypeError, ValueError):
        return default
    if not np.isfinite(num):
        return default
    return num


def _safe_int(value, default=0):
    num = _safe_float(value, None)
    if num is None:
        return default
    try:
        return int(num)
    except (TypeError, ValueError, OverflowError):
        return default


def _fmt_price(value):
    num = _safe_float(value, None)
    if num is None:
        return "N/A"
    return f"{num:.2f}"


def _fmt_int(value):
    num = _safe_int(value, None)
    if num is None:
        return "N/A"
    return f"{num}"


def _fmt_percent(value):
    num = _safe_float(value, None)
    if num is None:
        return "N/A"
    return f"{num:.2f}"


def _as_non_empty_text(value):
    if value is None:
        return ""
    return str(value).strip()


def _send_telegram_message(
    bot_token,
    chat_id,
    text,
    *,
    timeout_seconds=DEFAULT_TELEGRAM_TIMEOUT_SECONDS,
    logger_obj=None,
):
    logger_obj = logger_obj or logger
    url = f"https://api.telegram.org/bot{bot_token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        url,
        data=payload,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            status = getattr(response, "status", response.getcode())
            body = response.read().decode("utf-8", errors="ignore")
        if status != 200:
            logger_obj.error(
                "telegram sendMessage failed: status=%s body=%s",
                status,
                body[:240],
            )
            return False
        try:
            payload_data = json.loads(body) if body else {}
        except json.JSONDecodeError:
            payload_data = {}
        if payload_data.get("ok") is False:
            logger_obj.error(
                "telegram sendMessage rejected: %s",
                payload_data.get("description") or body[:240],
            )
            return False
        return True
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", errors="ignore")
        except Exception:
            body = str(e)
        logger_obj.error(
            "telegram sendMessage HTTP error: code=%s body=%s",
            getattr(e, "code", "?"),
            body[:240],
        )
    except Exception as e:
        logger_obj.error("telegram sendMessage error: %s", e)
    return False


def make_telegram_short_close_alert_handler(bot_token, chat_id, logger_obj=None):
    token = _as_non_empty_text(bot_token)
    chat = _as_non_empty_text(chat_id)
    if not token or not chat:
        return None
    logger_obj = logger_obj or logger

    def _handler(
        *,
        port,
        stock_code,
        option_code,
        count,
        pl_ratio,
        threshold,
    ):
        message = (
            "Short option close alert\n"
            f"stock={stock_code} port={port}\n"
            f"option={option_code}\n"
            f"count={abs(_safe_int(count, 0))} "
            f"profit={_safe_float(pl_ratio, 0.0):.2f}% "
            f"(threshold={_safe_float(threshold, 0.0):.2f}%)"
        )
        sent = _send_telegram_message(
            token,
            chat,
            message,
            logger_obj=logger_obj,
        )
        if sent:
            logger_obj.info(
                "telegram short close alert sent: stock=%s port=%s option=%s",
                stock_code,
                port,
                option_code,
            )
        return sent

    return _handler


def _option_side(option):
    # Prefer explicit side; fallback to count sign
    side = option.get("side")
    if side in (SIDE_SHORT, SIDE_LONG):
        return side
    count = _safe_int(option.get("count"), 0)
    return SIDE_SHORT if count < 0 else SIDE_LONG


def _infer_stock_price(strike_price, option_price, premium, option_type):
    # 通过期权参数反推标的价格；无法可靠计算时返回 None，由上层跳过该条数据
    strike = _safe_float(strike_price, None)
    price = _safe_float(option_price, None)
    prem = _safe_float(premium, None)
    if strike is None or price is None or prem is None:
        return None
    if option_type == OptionType.PUT:
        denominator = 1 - prem / 100
        if abs(denominator) < 1e-9:
            return None
        return (strike - price) / denominator
    denominator = 1 + prem / 100
    if abs(denominator) < 1e-9:
        return None
    return (strike + price) / denominator


def _price_fields_by_mode(price_mode, market_state=None):
    mode = (price_mode or "implied").lower()
    if mode == "implied":
        return []
    if mode == "last":
        return ["last_price"]
    if mode == "pre":
        return ["pre_price", "last_price"]
    if mode == "after":
        return ["after_price", "last_price"]
    if mode == "overnight":
        return ["overnight_price", "after_price", "last_price"]
    state = str(market_state or "").upper()
    if state in US_PRE_MARKET_STATES:
        return ["pre_price", "last_price", "after_price", "overnight_price"]
    if state in US_REGULAR_MARKET_STATES:
        return ["last_price", "pre_price", "after_price", "overnight_price"]
    if state in US_AFTER_HOURS_STATES:
        return ["after_price", "last_price", "overnight_price", "pre_price"]
    if state in US_OVERNIGHT_STATES:
        return ["overnight_price", "after_price", "last_price", "pre_price"]
    return ["last_price", "pre_price", "after_price", "overnight_price"]


def _pick_price_from_snapshot(data, fields):
    for field in fields:
        price = _safe_float(data.get(field), None)
        if price is not None and price > 0:
            return price
    return None


@contextmanager
def safe_quote_ctx(host, port):
    ctx = None
    try:
        logger.info("Initializing OpenQuoteContext.")
        ctx = OpenQuoteContext(host=host, port=port)
        logger.info("Initialized OpenQuoteContext.")
        yield ctx
    except Exception as e:
        logger.error("Init QuoteCtx exception: %s", e)
        raise
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception as e:
                logger.warning("Failed to close QuoteCtx cleanly: %s", e)


@contextmanager
def safe_trade_ctx(host, port, filter_trdmarket=TrdMarket.US):
    ctx = None
    try:
        logger.info("Initializing OpenSecTradeContext for US market.")
        ctx = OpenSecTradeContext(
            filter_trdmarket=filter_trdmarket,
            host=host,
            port=port,
            security_firm=SecurityFirm.FUTUSECURITIES,
        )
        logger.info("Initialized OpenSecTradeContext.")
        yield ctx
    except Exception as e:
        logger.error("Init TradeCtx exception: %s", e)
        raise
    finally:
        if ctx:
            try:
                ctx.close()
            except Exception as e:
                logger.warning("Failed to close TradeCtx cleanly: %s", e)


def _log_profit_hits(stock_code, hit_codes):
    prev = _last_profit_hit_codes.get(stock_code)
    if prev == hit_codes:
        return
    _last_profit_hit_codes[stock_code] = set(hit_codes)
    if hit_codes:
        preview = ", ".join(sorted(hit_codes)[:5])
        logger.info(f"profit_hit {stock_code}: {len(hit_codes)} codes -> {preview}")
    elif prev:
        logger.info(f"profit_hit {stock_code}: 0")


def _query_positions_with_log(trade_ctx, trade_lock=None, purpose=""):
    # 带日志与耗时统计的持仓查询
    start = time.perf_counter()
    logger.info(f"query_hold_positions start: {purpose}")
    try:
        if trade_lock is None:
            positions = query_hold_positions(trade_ctx)
        else:
            with trade_lock:
                positions = query_hold_positions(trade_ctx)
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.info(f"query_hold_positions done: {purpose}, cost={elapsed_ms:.1f}ms")
        return positions
    except Exception as e:
        elapsed_ms = (time.perf_counter() - start) * 1000
        logger.error(f"query_hold_positions error: {purpose}, cost={elapsed_ms:.1f}ms, err={e}")
        raise


def _build_stock_code_targets(stock_codes):
    full_code_targets = {}
    bare_code_targets = {}
    for stock_code in list(dict.fromkeys(stock_codes)):
        code_text = str(stock_code).strip().upper()
        if not code_text:
            continue
        bare_code = code_text.split(".")[-1]
        full_code_targets.setdefault(code_text, []).append(stock_code)
        bare_code_targets.setdefault(bare_code, []).append(stock_code)
    return full_code_targets, bare_code_targets


def _resolve_stock_targets(raw_code, full_code_targets, bare_code_targets):
    code_text = str(raw_code).strip().upper()
    if not code_text:
        return []
    targets = full_code_targets.get(code_text)
    if targets:
        return targets
    return bare_code_targets.get(code_text.split(".")[-1], [])


def _get_options_map_from_positions(positions, stock_codes):
    # 单次遍历持仓快照，按标的聚合期权，避免“每个标的都全表扫描”
    stock_codes = list(dict.fromkeys(stock_codes))
    options_map = {stock_code: [] for stock_code in stock_codes}
    hit_codes_map = {stock_code: set() for stock_code in stock_codes}
    full_code_targets, bare_code_targets = _build_stock_code_targets(stock_codes)

    if positions is None or positions.empty:
        for stock_code in stock_codes:
            _log_profit_hits(stock_code, set())
        return options_map

    for _, position in positions.iterrows():
        code = position["code"]
        count = _safe_int(position.get("qty"), 0)
        if count == 0:  # 忽略空仓
            continue
        side = SIDE_SHORT if count < 0 else SIDE_LONG
        code_segs = re.split(r'(\d+)', code)
        if len(code_segs) <= 3:  # not option
            continue
        target_stock_codes = _resolve_stock_targets(
            code_segs[0],
            full_code_targets,
            bare_code_targets,
        )
        if not target_stock_codes:
            continue
        if code_segs[2] == 'C':
            option_type = OptionEnum.CALL
        elif code_segs[2] == 'P':
            option_type = OptionEnum.PUT
        else:
            logger.error(f"Unknown option type in code {code}, skip.")
            continue
        pl_ratio = _safe_float(position.get("pl_ratio", 0), 0.0)
        pl_val = _safe_float(position.get("pl_val"), None)
        market_val = _safe_float(position.get("market_val"), None)
        option_item = {
            "code": code,
            "type": option_type,
            "side": side,
            "strike_date": code_segs[1],
            "strike_price": float(code_segs[3]) / 1000,
            "count": count,
            "pl_ratio": pl_ratio,
            "pl_val": pl_val,
            "market_val": market_val,
        }
        for stock_code in target_stock_codes:
            if pl_ratio >= PROFIT_HIGHLIGHT_THRESHOLD:
                hit_codes_map[stock_code].add(code)
            options_map[stock_code].append(dict(option_item))
    for stock_code in stock_codes:
        _log_profit_hits(stock_code, hit_codes_map[stock_code])
    return options_map


def get_stock_share_delta_map(positions, stock_codes):
    # 统计正股仓位 delta（1 股正股按 delta=1）
    stock_codes = list(dict.fromkeys(stock_codes))
    stock_delta_map = {stock_code: 0.0 for stock_code in stock_codes}
    if positions is None or positions.empty:
        return stock_delta_map
    full_code_targets, bare_code_targets = _build_stock_code_targets(stock_codes)
    for _, position in positions.iterrows():
        code = position.get("code")
        code_segs = re.split(r'(\d+)', str(code))
        if len(code_segs) > 3:  # option
            continue
        count = _safe_float(position.get("qty"), 0.0)
        if count == 0:
            continue
        target_stock_codes = _resolve_stock_targets(
            code,
            full_code_targets,
            bare_code_targets,
        )
        for stock_code in target_stock_codes:
            stock_delta_map[stock_code] += count
    return stock_delta_map


def _option_type_text(option_type):
    if option_type == OptionEnum.PUT:
        return "PUT"
    if option_type == OptionEnum.CALL:
        return "CALL"
    raw = str(option_type).upper()
    if "PUT" in raw:
        return "PUT"
    if "CALL" in raw:
        return "CALL"
    return ""


def get_option_position_counts(options):
    counts = {
        "short_call": 0,
        "short_put": 0,
        "long_call": 0,
        "long_put": 0,
    }
    for option in options or []:
        count = abs(_safe_int(option.get("count"), 0))
        if count == 0:
            continue
        side = _option_side(option)
        option_type = _option_type_text(option.get("type"))
        if side == SIDE_SHORT and option_type == "CALL":
            counts["short_call"] += count
        elif side == SIDE_SHORT and option_type == "PUT":
            counts["short_put"] += count
        elif side == SIDE_LONG and option_type == "CALL":
            counts["long_call"] += count
        elif side == SIDE_LONG and option_type == "PUT":
            counts["long_put"] += count
    return counts


def format_option_position_count_text(counts):
    counts = counts or {}
    return " | ".join(
        [
            f"short call: {_safe_int(counts.get('short_call'), 0)}",
            f"short put: {_safe_int(counts.get('short_put'), 0)}",
            f"long call: {_safe_int(counts.get('long_call'), 0)}",
            f"long put: {_safe_int(counts.get('long_put'), 0)}",
        ]
    )


def get_options_delta_sum(options):
    # 参考 turtle/find_positions.py: sum(count * option_delta * contract_size)
    total_delta = 0.0
    for option in options or []:
        count = _safe_int(option.get("count"), 0)
        if count == 0:
            continue
        delta = _safe_float(option.get("delta"), None)
        if delta is None:
            continue
        option_type = _option_type_text(option.get("type"))
        if count < 0 and delta == 0:
            # 对齐 turtle 逻辑：短仓且 API 返回 0 delta 时做保守兜底
            delta = 1.0 if option_type == "CALL" else -1.0
        contract_size = _safe_int(option.get("contract_size"), 100)
        if contract_size <= 0:
            contract_size = 100
        total_delta += count * delta * contract_size
    return total_delta


def get_options_short_value_sum(options):
    # 统计空头期权市值总和（按绝对值汇总，便于阅读）
    total_short_value = 0.0
    for option in options or []:
        count = _safe_int(option.get("count"), 0)
        if count >= 0:
            continue
        market_val = _safe_float(option.get("market_val"), None)
        if market_val is None:
            continue
        total_short_value += abs(market_val)
    return total_short_value


def _get_options_from_positions(positions, stock_code):
    options_map = _get_options_map_from_positions(positions, [stock_code])
    return options_map.get(stock_code, [])


def get_options(trade_ctx, stock_code, trade_lock=None, positions=None):
    # 支持传入持仓快照，避免重复查询
    if positions is None:
        positions = _query_positions_with_log(
            trade_ctx, trade_lock, purpose=f"get_options:{stock_code}"
        )
    return _get_options_from_positions(positions, stock_code)


def get_options_map(trade_ctx, stock_codes, trade_lock=None, positions=None):
    # 支持一次性返回多个标的的期权列表，避免重复扫描持仓快照
    if positions is None:
        positions = _query_positions_with_log(
            trade_ctx, trade_lock, purpose=f"get_options_map:{','.join(stock_codes)}"
        )
    return _get_options_map_from_positions(positions, stock_codes)


def _get_stock_prices_from_options_batch(quote_ctx, option_code_snapshot, quote_lock=None):
    # 批量用期权快照反推标的价格（implied），减少接口调用次数，并记录耗时
    t0 = time.perf_counter()  # 计时起点
    prices = {}
    option_to_stock = {}
    for stock_code, option_code in option_code_snapshot.items():
        if option_code:
            option_to_stock[option_code] = stock_code
    if not option_to_stock:
        return prices

    option_codes = list(option_to_stock.keys())
    logger.debug(f"get_market_snapshot batch start: codes={len(option_codes)}")
    page_size = 300  # futu api level2 limit
    for offset in range(0, len(option_codes), page_size):
        page_codes = option_codes[offset:offset + page_size]
        if quote_lock is None:
            ret_code, datas = quote_ctx.get_market_snapshot(page_codes)
        else:
            with quote_lock:
                ret_code, datas = quote_ctx.get_market_snapshot(page_codes)
        if ret_code != RET_OK:
            logger.error(f"get_market_snapshot batch failed: {ret_code}, {datas}")
            continue
        for _, data in datas.iterrows():
            option_code = data["code"]
            stock_code = option_to_stock.get(option_code)
            if not stock_code:
                continue
            stock_price = _infer_stock_price(
                data["option_strike_price"],
                data["last_price"],
                data["option_premium"],
                data["option_type"],
            )
            # 批量轮询中跳过异常行情，避免一个坏点影响全部标的更新
            if stock_price is None:
                continue
            prices[stock_code] = stock_price
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.debug(f"get_market_snapshot batch done: codes={len(option_codes)}, cost={elapsed_ms:.1f}ms")
    return prices


def _get_us_market_state(quote_ctx, quote_lock=None):
    # 用于 auto 模式下选择盘前/常规/盘后/夜盘字段优先级
    if quote_lock is None:
        ret_code, data = quote_ctx.get_global_state()
    else:
        with quote_lock:
            ret_code, data = quote_ctx.get_global_state()
    if ret_code != RET_OK:
        logger.debug(f"get_global_state failed: {ret_code}, {data}")
        return None
    if isinstance(data, dict):
        return data.get("us_market_state") or data.get("market_us")
    return None


def _get_stock_prices_from_snapshot_batch(
    quote_ctx, stock_codes, price_mode="implied", market_state=None, quote_lock=None
):
    # 批量从正股快照读取价格字段（last/pre/after/overnight）
    prices = {}
    unique_codes = list(dict.fromkeys(code for code in stock_codes if code))
    if not unique_codes:
        return prices
    fields = _price_fields_by_mode(price_mode, market_state=market_state)
    if not fields:
        return prices
    t0 = time.perf_counter()
    logger.debug(
        f"get_market_snapshot stock prices start: codes={len(unique_codes)}, "
        f"mode={price_mode}, market_state={market_state}, fields={fields}"
    )
    page_size = 300
    for offset in range(0, len(unique_codes), page_size):
        page_codes = unique_codes[offset:offset + page_size]
        if quote_lock is None:
            ret_code, datas = quote_ctx.get_market_snapshot(page_codes)
        else:
            with quote_lock:
                ret_code, datas = quote_ctx.get_market_snapshot(page_codes)
        if ret_code != RET_OK:
            logger.error(f"get_market_snapshot stock prices failed: {ret_code}, {datas}")
            continue
        for _, data in datas.iterrows():
            code = data.get("code")
            if not code:
                continue
            price = _pick_price_from_snapshot(data, fields)
            if price is None:
                continue
            prices[code] = price
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.debug(
        f"get_market_snapshot stock prices done: codes={len(unique_codes)}, "
        f"matched={len(prices)}, cost={elapsed_ms:.1f}ms"
    )
    return prices


def _get_stock_prices_with_fallback(
    quote_ctx, stock_codes, option_code_snapshot, price_mode="implied", quote_lock=None
):
    # 先取正股快照（支持盘前/盘后字段），缺失时回退到期权 implied 价格
    prices = {}
    if price_mode != "implied":
        market_state = None
        if price_mode == "auto":
            market_state = _get_us_market_state(quote_ctx, quote_lock=quote_lock)
        prices = _get_stock_prices_from_snapshot_batch(
            quote_ctx,
            stock_codes,
            price_mode=price_mode,
            market_state=market_state,
            quote_lock=quote_lock,
        )
    missing_codes = [code for code in stock_codes if code not in prices]
    if not missing_codes:
        return prices
    missing_option_snapshot = {
        stock_code: option_code_snapshot.get(stock_code)
        for stock_code in missing_codes
        if option_code_snapshot.get(stock_code)
    }
    if not missing_option_snapshot:
        return prices
    implied_prices = _get_stock_prices_from_options_batch(
        quote_ctx, missing_option_snapshot, quote_lock=quote_lock
    )
    if implied_prices:
        logger.debug(
            f"price fallback implied used: mode={price_mode}, "
            f"missing={len(missing_codes)}, recovered={len(implied_prices)}"
        )
        prices.update(implied_prices)
    return prices


def _get_option_quotes_batch(quote_ctx, option_codes, quote_lock=None):
    # 批量查询期权盘口字段，用于悬停提示
    quotes = {}
    unique_codes = list(dict.fromkeys(code for code in option_codes if code))
    if not unique_codes:
        return quotes
    t0 = time.perf_counter()
    logger.debug(f"get_market_snapshot option quotes start: codes={len(unique_codes)}")
    page_size = 300  # futu api level2 limit
    for offset in range(0, len(unique_codes), page_size):
        page_codes = unique_codes[offset:offset + page_size]
        if quote_lock is None:
            ret_code, datas = quote_ctx.get_market_snapshot(page_codes)
        else:
            with quote_lock:
                ret_code, datas = quote_ctx.get_market_snapshot(page_codes)
        if ret_code != RET_OK:
            logger.error(f"get_market_snapshot option quotes failed: {ret_code}, {datas}")
            continue
        for _, data in datas.iterrows():
            code = data.get("code")
            if not code:
                continue
            quotes[code] = {
                "price": _safe_float(data.get("last_price"), None),
                "bid_price": _safe_float(data.get("bid_price"), None),
                "ask_price": _safe_float(data.get("ask_price"), None),
                "volume": _safe_int(data.get("volume"), None),
                "open_interest": _safe_int(
                    data.get("option_open_interest", data.get("open_interest")), None
                ),
                "delta": _safe_float(data.get("option_delta"), None),
                "contract_size": _safe_int(data.get("option_contract_size"), 100),
            }
    elapsed_ms = (time.perf_counter() - t0) * 1000
    logger.debug(
        f"get_market_snapshot option quotes done: codes={len(unique_codes)}, cost={elapsed_ms:.1f}ms"
    )
    return quotes


def _merge_option_quotes(options, quotes):
    # 将快照字段附加到期权字典，便于绘图悬停展示
    for option in options:
        quote = quotes.get(option.get("code"), {})
        option["price"] = quote.get("price")
        option["bid_price"] = quote.get("bid_price")
        option["ask_price"] = quote.get("ask_price")
        option["volume"] = quote.get("volume")
        option["open_interest"] = quote.get("open_interest")
        option["delta"] = quote.get("delta")
        option["contract_size"] = quote.get("contract_size")


def _options_signature(options):
    # 用于判断散点几何是否变化；仅这些字段变化才需要重绘
    signature = []
    for option in options:
        pl_ratio = _safe_float(option.get("pl_ratio", 0), 0.0)
        profit_hit = pl_ratio >= PROFIT_HIGHLIGHT_THRESHOLD  # 命中阈值则触发重绘
        signature.append(
            (
                option.get("code"),
                int(option.get("count", 0)),
                option.get("strike_date"),
                float(option.get("strike_price", 0)),
                str(option.get("type")),
                _option_side(option),
                profit_hit,
            )
        )
    return tuple(sorted(signature))


def _options_hover_signature(options):
    # 用于判断悬停信息是否变化；变化时只更新 tooltip 数据，不触发重绘
    signature = []
    for option in options:
        price = _safe_float(option.get("price"), None)
        bid_price = _safe_float(option.get("bid_price"), None)
        ask_price = _safe_float(option.get("ask_price"), None)
        volume = _safe_int(option.get("volume"), None)
        open_interest = _safe_int(option.get("open_interest"), None)
        profit_ratio = _safe_float(option.get("pl_ratio"), None)
        profit_value = _safe_float(option.get("pl_val"), None)
        signature.append(
            (
                option.get("code"),
                int(option.get("count", 0)),
                None if price is None else round(price, 4),
                None if bid_price is None else round(bid_price, 4),
                None if ask_price is None else round(ask_price, 4),
                volume,
                open_interest,
                None if profit_ratio is None else round(profit_ratio, 4),
                None if profit_value is None else round(profit_value, 4),
            )
        )
    return tuple(sorted(signature))


def _panel_key(port_index, stock_code):
    return (port_index, stock_code)


def _panel_title(stock_code, port, delta_sum=None, short_value=None):
    title = f"{stock_code} Option Positions (Port {port})"
    metrics = []
    if delta_sum is not None:
        metrics.append(f"delta={_safe_float(delta_sum, 0.0):+.3f}")
    if short_value is not None:
        metrics.append(f"short_value={_safe_float(short_value, 0.0):.2f}")
    if not metrics:
        return title
    return f"{title} | {' | '.join(metrics)}"


def _pick_price_option_code(stock_code, option_code_by_panel, port_count):
    # 同一股票价格线在左右图一致：优先取最左侧端口可用期权作为取价锚点
    for port_index in range(port_count):
        code = option_code_by_panel.get(_panel_key(port_index, stock_code))
        if code:
            return code
    return None
