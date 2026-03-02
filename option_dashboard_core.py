import logging
import re
import sys
import time
from contextlib import contextmanager

import numpy as np
from futu import (RET_OK, OpenQuoteContext, OpenSecTradeContext, OptionType,
                  SecurityFirm, TrdMarket)

from options import OptionEnum
from positions import query_hold_positions

logger = logging.getLogger("plot_positions_option")

PROFIT_HIGHLIGHT_THRESHOLD = 80.0  # Highlight threshold
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


def _get_options_map_from_positions(positions, stock_codes):
    # 单次遍历持仓快照，按标的聚合期权，避免“每个标的都全表扫描”
    stock_codes = list(dict.fromkeys(stock_codes))
    options_map = {stock_code: [] for stock_code in stock_codes}
    hit_codes_map = {stock_code: set() for stock_code in stock_codes}
    if positions is None or positions.empty:
        for stock_code in stock_codes:
            _log_profit_hits(stock_code, set())
        return options_map

    stock_code_set = set(stock_codes)
    for _, position in positions.iterrows():
        code = position["code"]
        count = _safe_int(position.get("qty"), 0)
        if count == 0:  # 忽略空仓
            continue
        side = SIDE_SHORT if count < 0 else SIDE_LONG
        code_segs = re.split(r'(\d+)', code)
        if len(code_segs) <= 3:  # not option
            continue
        stock_code = code_segs[0]
        if stock_code not in stock_code_set:
            continue
        if code_segs[2] == 'C':
            option_type = OptionEnum.CALL
        elif code_segs[2] == 'P':
            option_type = OptionEnum.PUT
        else:
            logger.error(f"Unknown option type in code {code}, skip.")
            continue
        pl_ratio = _safe_float(position.get("pl_ratio", 0), 0.0)
        if pl_ratio >= PROFIT_HIGHLIGHT_THRESHOLD:
            hit_codes_map[stock_code].add(code)
        options_map[stock_code].append({
            "code": code,
            "type": option_type,
            "side": side,
            "strike_date": code_segs[1],
            "strike_price": float(code_segs[3]) / 1000,
            "count": count,
            "pl_ratio": pl_ratio,
        })
    for stock_code in stock_codes:
        _log_profit_hits(stock_code, hit_codes_map[stock_code])
    return options_map


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
            )
        )
    return tuple(sorted(signature))


def _panel_key(port_index, stock_code):
    return (port_index, stock_code)


def _panel_title(stock_code, port):
    return f"{stock_code} Option Positions (Port {port})"


def _pick_price_option_code(stock_code, option_code_by_panel, port_count):
    # 同一股票价格线在左右图一致：优先取最左侧端口可用期权作为取价锚点
    for port_index in range(port_count):
        code = option_code_by_panel.get(_panel_key(port_index, stock_code))
        if code:
            return code
    return None
