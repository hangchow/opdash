import argparse
import datetime
import logging
import re
import sys
import time

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import mplcursors
import numpy as np
from matplotlib.legend_handler import HandlerTuple
from matplotlib.lines import Line2D

logger = logging.getLogger("plot_positions_option")  # 固定日志名，避免显示为 __main__
from contextlib import ExitStack, contextmanager
from threading import Event, Lock, Thread

from futu import (RET_OK, OpenQuoteContext, OpenSecTradeContext, OptionType,
                  SecurityFirm, TrdMarket)

from options import OptionEnum
from positions import query_hold_positions

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

# 本脚本不再依赖 futu.yaml，改用命令行指定 host/port
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(threadName)s - %(levelname)s - %(message)s"  # 日志以线程名为主，避免 __main__ 噪音
)


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


def parse_args():
    parser = argparse.ArgumentParser(
        description="Parameter settings for runing %(prog)s" % {'prog': sys.argv[0]}
    )

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
        help="ui refresh interval seconds (default: 5)",
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

    # 覆写 error 方法，输出错误信息后退出
    def custom_error(message):
        sys.stderr.write(f"Error: {message}\n\n")
        parser.print_help()
        sys.exit(1)
    parser.error = custom_error

    args = parser.parse_args()
    stock_codes = args.stock_codes
    host = args.host
    ports = []
    for raw_port in str(args.port).split(","):
        raw_port = raw_port.strip()
        if not raw_port:
            continue
        try:
            ports.append(int(raw_port))
        except ValueError:
            parser.error(f"--port contains non-integer value: {raw_port}")
    if not ports:
        parser.error("--port must provide at least one valid integer port")
    if len(ports) > 2:
        logger.warning(f"--port provided {len(ports)} values, only first two will be used: {ports[:2]}")
    ports = ports[:2]
    poll_interval = args.poll_interval
    price_interval = args.price_interval
    ui_interval = args.ui_interval
    price_mode = args.price_mode
    return stock_codes, host, ports, poll_interval, price_interval, ui_interval, price_mode

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
            optionType = OptionEnum.CALL
        elif code_segs[2] == 'P':
            optionType = OptionEnum.PUT
        else:
            logger.error(f"Unknown option type in code {code}, skip.")
            continue
        pl_ratio = _safe_float(position.get("pl_ratio", 0), 0.0)
        if pl_ratio >= PROFIT_HIGHLIGHT_THRESHOLD:
            hit_codes_map[stock_code].add(code)
        options_map[stock_code].append({
            "code": code,
            "type": optionType,
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

def get_stock_data(quote_ctx, stock_code, option_code, max_retries=999, quote_lock=None):
    retries = 0
    while retries < max_retries:
        if quote_lock is None:
            ret_code, datas = quote_ctx.get_market_snapshot(option_code)
        else:
            with quote_lock:
                ret_code, datas = quote_ctx.get_market_snapshot(option_code)
        if ret_code == RET_OK:
            for i, data in datas.iterrows():
                stock_price = _infer_stock_price(
                    data["option_strike_price"],
                    data["last_price"],
                    data["option_premium"],
                    data["option_type"],
                )
                # 单条快照无效时跳过，继续尝试同一批次中的下一条
                if stock_price is None:
                    continue
                return {"code":stock_code, "price":stock_price}
        logger.error(f"Failed to get_market_snapshot({option_code}), ret_code: {ret_code}, error: {datas}")
        time.sleep(1.5)
        retries += 1
    raise Exception(f"Failed to get_market_snapshot({option_code}) after {max_retries} retries")

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
    logger.debug(f"get_market_snapshot batch start: codes={len(option_codes)}")  # 开始批量查询价格
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
        for i, data in datas.iterrows():
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


def _point_counts_from_options(options):
    # 从期权列表生成悬停详情映射（按行权日+行权价）
    point_counts = {}
    for option in options:
        strike_dt = datetime.datetime.strptime(option["strike_date"], "%y%m%d")
        strike_price = _safe_float(option.get("strike_price"), 0.0)
        key = (round(mdates.date2num(strike_dt), 8), round(strike_price, 6))
        detail = {
            "count": abs(int(option.get("count", 1))),
            "type": option.get("type"),
            "side": _option_side(option),
            "price": option.get("price"),
            "bid_price": option.get("bid_price"),
            "ask_price": option.get("ask_price"),
            "volume": option.get("volume"),
            "open_interest": option.get("open_interest"),
            "profit_ratio": option.get("pl_ratio"),
        }
        entry = point_counts.setdefault(key, [])
        entry.append(detail)
    return point_counts


def _compute_plot_data(options):
    # 将期权列表转成绘图数据，避免在多个地方重复计算
    put_x = []
    put_y = []
    put_s = []
    put_edgecolors = []
    put_facecolors = []
    call_x = []
    call_y = []
    call_s = []
    call_edgecolors = []
    call_facecolors = []
    for option in options:
        count = abs(int(option.get("count", 1)))
        size = 40 + max(0, count - 1) * 20
        strike_dt = datetime.datetime.strptime(option["strike_date"], "%y%m%d")
        pl_ratio = _safe_float(option.get("pl_ratio", 0), 0.0)
        profit_hit = pl_ratio >= PROFIT_HIGHLIGHT_THRESHOLD  # 仅高亮达标点
        side = _option_side(option)
        edge_color = SHORT_POSITION_COLOR if side == SIDE_SHORT else LONG_POSITION_COLOR
        face_color = edge_color if profit_hit else HOLLOW_FACE_COLOR
        if option["type"] == OptionEnum.PUT:
            put_x.append(strike_dt)
            put_y.append(option["strike_price"])
            put_s.append(size)
            put_edgecolors.append(edge_color)
            put_facecolors.append(face_color)
        else:
            call_x.append(strike_dt)
            call_y.append(option["strike_price"])
            call_s.append(size)
            call_edgecolors.append(edge_color)
            call_facecolors.append(face_color)
    point_counts = _point_counts_from_options(options)
    unique_dates = sorted(set(call_x + put_x))
    y_all = [option["strike_price"] for option in options]
    return {
        "call_x": call_x,
        "call_y": call_y,
        "call_s": call_s,
        "call_edgecolors": call_edgecolors,
        "call_facecolors": call_facecolors,
        "put_x": put_x,
        "put_y": put_y,
        "put_s": put_s,
        "put_edgecolors": put_edgecolors,
        "put_facecolors": put_facecolors,
        "point_counts": point_counts,
        "unique_dates": unique_dates,
        "y_all": y_all,
    }

def _to_offsets(x_list, y_list):
    # matplotlib scatter 需要 (x, y) offsets
    if not x_list:
        return np.empty((0, 2))
    return np.column_stack((x_list, y_list))


def _add_marker_legend(ax):
    # In-chart legend for shape/color semantics
    sell_filled_combo = (
        Line2D(
            [],
            [],
            linestyle="None",
            marker="o",
            markerfacecolor=SHORT_POSITION_COLOR,
            markeredgecolor=SHORT_POSITION_COLOR,
            markersize=8,
        ),
        Line2D(
            [],
            [],
            linestyle="None",
            marker="^",
            markerfacecolor=SHORT_POSITION_COLOR,
            markeredgecolor=SHORT_POSITION_COLOR,
            markersize=8,
        ),
    )
    legend_handles = [
        Line2D(
            [],
            [],
            linestyle="None",
            marker="o",
            markerfacecolor="none",
            markeredgecolor=SHORT_POSITION_COLOR,
            markersize=8,
            label="Short Call",
        ),
        Line2D(
            [],
            [],
            linestyle="None",
            marker="o",
            markerfacecolor="none",
            markeredgecolor=LONG_POSITION_COLOR,
            markersize=8,
            label="Long Call",
        ),
        Line2D(
            [],
            [],
            linestyle="None",
            marker="^",
            markerfacecolor="none",
            markeredgecolor=SHORT_POSITION_COLOR,
            markersize=8,
            label="Short Put",
        ),
        Line2D(
            [],
            [],
            linestyle="None",
            marker="^",
            markerfacecolor="none",
            markeredgecolor=LONG_POSITION_COLOR,
            markersize=8,
            label="Long Put",
        ),
        sell_filled_combo,
    ]
    legend_labels = [
        "Short Call",
        "Long Call",
        "Short Put",
        "Long Put",
        f"profit% >= {PROFIT_HIGHLIGHT_THRESHOLD:.0f}%",
    ]
    ax.legend(
        handles=legend_handles,
        labels=legend_labels,
        loc="upper left",
        fontsize=9,
        framealpha=0.9,
        handler_map={tuple: HandlerTuple(ndivide=None)},
    )

def plot_chart(ax, options, stock_code, stock_price=None, chart_title=None, show_legend=False):
    # 初始化绘图：散点 + 悬停 + 基准线
    plot_data = _compute_plot_data(options)
    call_x = plot_data["call_x"]
    call_y = plot_data["call_y"]
    call_s = plot_data["call_s"]
    put_x = plot_data["put_x"]
    put_y = plot_data["put_y"]
    put_s = plot_data["put_s"]
    call_edgecolors = plot_data["call_edgecolors"]
    call_facecolors = plot_data["call_facecolors"]
    put_edgecolors = plot_data["put_edgecolors"]
    put_facecolors = plot_data["put_facecolors"]
    point_counts = plot_data["point_counts"]
    # plot scatter points
    call_sc = ax.scatter(call_x, call_y, edgecolors=call_edgecolors, facecolors=call_facecolors, s=call_s, marker='o')
    put_sc = ax.scatter(put_x, put_y, edgecolors=put_edgecolors, facecolors=put_facecolors, s=put_s, marker='^')
    unique_dates = plot_data["unique_dates"]
    if unique_dates:
        ax.set_xticks(unique_dates)
        ax.set_xticklabels(
            [d.strftime("%Y-%m-%d") for d in unique_dates],
            rotation=45,
            ha="right",
            rotation_mode="anchor",
        )
    ax.set_xlabel("Strike Date")
    ax.set_ylabel("Strike Price")
    ax.set_title(chart_title or f"{stock_code} Option Positions")
    if show_legend:
        _add_marker_legend(ax)

    state = {
        "call_sc": call_sc,
        "put_sc": put_sc,
        "point_counts": point_counts,
        "last_annotation": None,
        "cursor": None,
    }

    # enable hover annotations
    cursor = mplcursors.cursor([call_sc, put_sc], hover=True)
    state["cursor"] = cursor
    @cursor.connect("add")
    def on_hover(sel):
        x_num = round(float(sel.target[0]), 8)
        y_val = round(float(sel.target[1]), 6)
        strike_dt = mdates.num2date(sel.target[0]).strftime("%Y-%m-%d")  # 悬停提示里显示行权日
        details = point_counts.get((x_num, y_val), [])
        if details:
            lines = [f"{strike_dt}, {sel.target[1]:.2f}"]  # 第一行简化为 x,y
            for detail in sorted(
                details,
                key=lambda d: (
                    0 if d.get("side") == SIDE_SHORT else 1,
                    0 if d.get("type") == OptionEnum.CALL else 1,
                ),
            ):
                side_text = "SHORT" if detail.get("side") == SIDE_SHORT else "LONG"
                type_text = "CALL" if detail.get("type") == OptionEnum.CALL else "PUT"
                lines.append(
                    f"{side_text} {type_text}: "
                    f"count={detail['count']}, price={_fmt_price(detail.get('price'))}, "
                    f"bid_price={_fmt_price(detail.get('bid_price'))}, ask_price={_fmt_price(detail.get('ask_price'))}, "
                    f"volume={_fmt_int(detail.get('volume'))}, oi={_fmt_int(detail.get('open_interest'))}, "
                    f"profit%={_fmt_percent(detail.get('profit_ratio'))}"
                )
            sel.annotation.set_text("\n".join(lines))
        else:
            sel.annotation.set_text(f"{strike_dt}, {sel.target[1]:.2f}")  # 第一行简化为 x,y
        sel.annotation.get_bbox_patch().set(fc="white", alpha=0.8)
        state["last_annotation"] = sel.annotation

    # draw stock price line(red line)
    base_line, base_text = (None, None)
    if stock_price is not None:
        logger.info(f"chart {stock_code} init base line at y={stock_price}")  # 初次绘制红线及价格
        base_line, base_text = draw_base_line(ax, plot_data["y_all"], stock_price)
    return base_line, base_text, state

def update_plot(ax, options, state):
    # 仅更新散点数据与坐标轴，不重建对象
    plot_data = _compute_plot_data(options)
    call_sc = state["call_sc"]
    put_sc = state["put_sc"]
    call_sc.set_offsets(_to_offsets(plot_data["call_x"], plot_data["call_y"]))
    put_sc.set_offsets(_to_offsets(plot_data["put_x"], plot_data["put_y"]))
    call_sc.set_sizes(plot_data["call_s"])
    put_sc.set_sizes(plot_data["put_s"])
    call_sc.set_edgecolors(plot_data["call_edgecolors"])
    put_sc.set_edgecolors(plot_data["put_edgecolors"])
    call_sc.set_facecolors(plot_data["call_facecolors"])
    put_sc.set_facecolors(plot_data["put_facecolors"])
    has_data = bool(plot_data["y_all"])
    call_sc.set_visible(has_data)
    put_sc.set_visible(has_data)
    cursor = state.get("cursor")
    if cursor is not None and hasattr(cursor, "enabled"):
        cursor.enabled = has_data
    if not has_data:
        last_annotation = state.get("last_annotation")
        if last_annotation is not None:
            last_annotation.set_visible(False)
            state["last_annotation"] = None
    state["point_counts"].clear()
    state["point_counts"].update(plot_data["point_counts"])
    unique_dates = plot_data["unique_dates"]
    ax.set_xticks(unique_dates)
    ax.set_xticklabels(
        [d.strftime("%Y-%m-%d") for d in unique_dates],
        rotation=45,
        ha="right",
        rotation_mode="anchor",
    )
    # 注意：relim 对 scatter 支持不完整，容易导致坐标范围错乱、看起来“图形丢失”
    if unique_dates:
        x_min_num = mdates.date2num(min(unique_dates))
        x_max_num = mdates.date2num(max(unique_dates))
        x_pad = max(1.0, (x_max_num - x_min_num) * 0.1)  # 至少 1 天
        ax.set_xlim(
            mdates.num2date(x_min_num - x_pad),
            mdates.num2date(x_max_num + x_pad),
        )
    y_all = plot_data["y_all"]
    if y_all:
        y_min, y_max = min(y_all), max(y_all)
        y_pad = (y_max - y_min) * 0.1 or 1.0
        ax.set_ylim(y_min - y_pad, y_max + y_pad)

def draw_base_line(ax, y, price):
    # 绘制当前股价基准线
    base_y = round(price, 2)
    line = ax.axhline(y=base_y, color='red', linestyle='--', linewidth=1)
    # 文本使用 x 轴坐标(轴坐标系) + y 轴坐标(数据坐标系)，避免缩放时“位置漂移”
    text = ax.text(-0.01, base_y, f'y={base_y:.2f}',
            color='red', fontsize=10, ha='right', va='center', transform=ax.get_yaxis_transform())
    return line, text

def move_base_line(ax, line, text, new_y):
    new_y_round = round(new_y, 2)
    curr_y = line.get_ydata()[0]
    if round(curr_y, 2) == new_y_round:
        # 线不动时，文字也不更新，避免“数值/位置跳动”
        return False
    # 1) 改水平线的位置：Line2D 需要两端点的 y 值
    line.set_ydata([new_y_round, new_y_round])
    # 2) 更新文字位置与内容（y 用数据坐标系）
    text.set_position((-0.01, new_y_round))
    text.set_text(f'y={new_y_round:.2f}')
    return True


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


def maximize_figure_window(fig):
    # 尽量在不同 GUI 后端下最大化窗口，失败时静默降级
    try:
        manager = fig.canvas.manager
    except Exception:
        return
    try:
        if hasattr(manager, "window"):
            window = manager.window
            if hasattr(window, "state"):
                # TkAgg
                window.state("zoomed")
                return
            if hasattr(window, "showMaximized"):
                # QtAgg
                window.showMaximized()
                return
            if hasattr(window, "Maximize"):
                # WxAgg
                window.Maximize(True)
                return
        if hasattr(manager, "full_screen_toggle"):
            # 兜底：部分后端只有全屏切换
            manager.full_screen_toggle()
    except Exception as e:
        logger.debug(f"maximize window skipped: {e}")


if __name__ == "__main__":
    (
        stock_codes_str,
        host,
        ports,
        poll_interval,
        price_interval,
        ui_interval,
        price_mode,
    ) = parse_args()
    # 支持 "US.AAPL, US.TSLA" 这类输入，去掉空格并过滤空项
    stock_codes = [s.strip() for s in stock_codes_str.split(",") if s.strip()]
    if not stock_codes:
        logger.error("No valid stock codes provided. Example: US.AAPL,US.TSLA")
        sys.exit(1)
    port_count = len(ports)
    row_count = len(stock_codes)

    # 多端口时：列=端口，行=股票代码
    fig, axs = plt.subplots(
        row_count,
        port_count,
        figsize=(max(10, 9 * port_count), max(6, 3.8 * row_count)),
        sharex=False,
        squeeze=False,
    )

    try:
        with ExitStack() as stack:
            trade_ctxs = {}
            quote_ctxs = {}
            trade_locks = {}
            quote_locks = {}
            for port in ports:
                trade_ctxs[port] = stack.enter_context(safe_trade_ctx(host, port))
                quote_ctxs[port] = stack.enter_context(safe_quote_ctx(host, port))
                trade_locks[port] = Lock()
                quote_locks[port] = Lock()

            # 初始化每个端口、每个股票的期权数据
            initial_options_by_panel = {}
            initial_option_code_by_panel = {}
            initial_plot_signatures = {}
            initial_hover_signatures = {}
            initial_price_option_codes = {}

            for port_index, port in enumerate(ports):
                trade_ctx = trade_ctxs[port]
                quote_ctx = quote_ctxs[port]
                trade_lock = trade_locks[port]
                quote_lock = quote_locks[port]

                positions_snapshot = _query_positions_with_log(
                    trade_ctx, trade_lock, purpose=f"init:{port}"
                )
                options_snapshot = get_options_map(
                    trade_ctx, stock_codes, positions=positions_snapshot
                )

                option_quotes = _get_option_quotes_batch(
                    quote_ctx,
                    [
                        option["code"]
                        for options in options_snapshot.values()
                        for option in options
                    ],
                    quote_lock=quote_lock,
                )
                for options in options_snapshot.values():
                    _merge_option_quotes(options, option_quotes)

                for stock_code in stock_codes:
                    key = _panel_key(port_index, stock_code)
                    options = options_snapshot.get(stock_code, [])
                    option_code = options[0]["code"] if options else None
                    initial_options_by_panel[key] = options
                    initial_option_code_by_panel[key] = option_code
                    initial_plot_signatures[key] = _options_signature(options)
                    initial_hover_signatures[key] = _options_hover_signature(options)
                    if stock_code not in initial_price_option_codes and option_code:
                        initial_price_option_codes[stock_code] = option_code

            # 同一股票价格线左右图共用：统一使用一个线程批量轮询
            price_source_port = ports[0]
            price_quote_ctx = quote_ctxs[price_source_port]
            price_quote_lock = quote_locks[price_source_port]
            initial_prices = _get_stock_prices_with_fallback(
                price_quote_ctx,
                stock_codes,
                initial_price_option_codes,
                price_mode=price_mode,
                quote_lock=price_quote_lock,
            )

            base_lines = {}  # (port_index, stock_code) -> (line, text)
            plot_states = {}  # (port_index, stock_code) -> state
            last_drawn_prices = {}  # (port_index, stock_code) -> price
            legend_drawn = False
            for port_index, port in enumerate(ports):
                for row_index, stock_code in enumerate(stock_codes):
                    key = _panel_key(port_index, stock_code)
                    ax = axs[row_index][port_index]
                    options = initial_options_by_panel.get(key, [])
                    stock_price = initial_prices.get(stock_code)

                    if not options:
                        logger.warning(f"No option positions for {stock_code} on port {port}.")
                        ax.set_xlabel("Strike Date")
                        ax.set_ylabel("Strike Price")
                        ax.set_title(_panel_title(stock_code, port))
                        base_lines[key] = (None, None)
                        plot_states[key] = None
                        continue

                    base_line, base_text, state = plot_chart(
                        ax,
                        options,
                        stock_code,
                        stock_price,
                        chart_title=_panel_title(stock_code, port),
                        show_legend=not legend_drawn,
                    )
                    legend_drawn = True
                    base_lines[key] = (base_line, base_text)
                    plot_states[key] = state
                    if base_line is not None and stock_price is not None:
                        last_drawn_prices[key] = stock_price

            fig.tight_layout()
            maximize_figure_window(fig)
            fig.canvas.draw()  # 初次完整绘制

            # ===== 后台取stock线程：把最新stock数据（主要是价格）写入共享内存 =====
            stop_event = Event()
            price_lock = Lock()
            latest_prices = {}     # stock_code -> 最新价格（左右图共用）
            latest_options = {}    # (port_index, stock_code) -> 最新期权列表
            latest_options_sig = {}# (port_index, stock_code) -> 最近轮询到的散点签名
            latest_hover_sig = {}  # (port_index, stock_code) -> 最近轮询到的悬停签名
            last_drawn_options = {}# (port_index, stock_code) -> 上一次已画到图上的散点签名
            last_hover_options = {}# (port_index, stock_code) -> 上一次已同步到悬停框的签名
            latest_option_code = {}# (port_index, stock_code) -> 当前用于取价候选的期权代码
            latest_price_option_code = {}  # stock_code -> 统一取价候选期权代码
            options_lock = Lock()  # 期权列表线程安全
            options_version = {"value": 0}  # 期权快照版本（变化时+1）
            price_version = {"value": 0}    # 价格快照版本（变化时+1）
            last_handled_options_version = {"value": -1}
            last_handled_price_version = {"value": -1}
            latest_prices.update(initial_prices)  # 初始化价格
            latest_options_sig.update(initial_plot_signatures)
            latest_hover_sig.update(initial_hover_signatures)
            last_drawn_options.update(initial_plot_signatures)  # 初始化已绘制散点签名
            last_hover_options.update(initial_hover_signatures)  # 初始化已同步悬停签名
            latest_option_code.update(initial_option_code_by_panel)
            for stock_code in stock_codes:
                latest_price_option_code[stock_code] = _pick_price_option_code(
                    stock_code, latest_option_code, port_count
                )

            def poll_price_all(interval=5):
                # 单线程 + 批量接口轮询所有标的（左右图共用同一价格源）
                while not stop_event.is_set():
                    try:
                        with options_lock:
                            option_code_snapshot = {
                                stock_code: option_code
                                for stock_code, option_code in latest_price_option_code.items()
                                if option_code
                            }
                        prices = _get_stock_prices_with_fallback(
                            price_quote_ctx,
                            stock_codes,
                            option_code_snapshot,
                            price_mode=price_mode,
                            quote_lock=price_quote_lock,
                        )
                        if prices:
                            with price_lock:
                                latest_prices.update(prices)
                                price_version["value"] += 1
                            for stock_code, price in prices.items():
                                logger.debug(f"poll {stock_code} with price {price}")
                    except Exception as e:
                        logger.error(f"poll price error: {e}")
                    finally:
                        stop_event.wait(interval)

            workers = []
            t = Thread(target=poll_price_all, args=(price_interval,), daemon=True, name="poll_price")  # 价格轮询线程
            t.start()
            workers.append(t)

            # 每个端口一个期权轮询线程；价格线程仍保持单线程
            def poll_options_by_port(port_index, port, interval=10):
                trade_ctx = trade_ctxs[port]
                quote_ctx = quote_ctxs[port]
                trade_lock = trade_locks[port]
                quote_lock = quote_locks[port]
                while not stop_event.is_set():
                    try:
                        positions_snapshot = _query_positions_with_log(
                            trade_ctx, trade_lock, purpose=f"poll_options:{port}"
                        )
                        options_snapshot = get_options_map(
                            trade_ctx, stock_codes, positions=positions_snapshot
                        )
                        option_code_snapshot = {
                            stock_code: (options[0]["code"] if options else None)
                            for stock_code, options in options_snapshot.items()
                        }
                        option_quotes = _get_option_quotes_batch(
                            quote_ctx,
                            [
                                option["code"]
                                for options in options_snapshot.values()
                                for option in options
                            ],
                            quote_lock=quote_lock,
                        )
                        for options in options_snapshot.values():
                            _merge_option_quotes(options, option_quotes)
                        options_sig_snapshot = {
                            stock_code: _options_signature(options)
                            for stock_code, options in options_snapshot.items()
                        }
                        hover_sig_snapshot = {
                            stock_code: _options_hover_signature(options)
                            for stock_code, options in options_snapshot.items()
                        }
                        with options_lock:
                            for stock_code in stock_codes:
                                key = _panel_key(port_index, stock_code)
                                options = options_snapshot.get(stock_code, [])
                                latest_options[key] = options
                                latest_option_code[key] = option_code_snapshot.get(stock_code)
                                latest_options_sig[key] = options_sig_snapshot.get(
                                    stock_code, ()
                                )
                                latest_hover_sig[key] = hover_sig_snapshot.get(
                                    stock_code, ()
                                )
                            for stock_code in stock_codes:
                                latest_price_option_code[stock_code] = _pick_price_option_code(
                                    stock_code, latest_option_code, port_count
                                )
                            options_version["value"] += 1
                    except Exception as e:
                        logger.error(f"poll options error on port {port}: {e}")
                    finally:
                        stop_event.wait(interval)

            for port_index, port in enumerate(ports):
                t = Thread(
                    target=poll_options_by_port,
                    args=(port_index, port, poll_interval),
                    daemon=True,
                    name=f"poll_options_{port}",
                )
                t.start()
                workers.append(t)

            # 窗口关闭时优雅退出线程
            def _on_close(event):
                stop_event.set()
                # 给一点时间让线程退出
                for t in workers:
                    t.join(timeout=1.0)

            fig.canvas.mpl_connect('close_event', _on_close)  

            # ===== 用计时器驱动更新（不阻塞 GUI）=====
            def on_timer():
                with price_lock:
                    # 拷贝一份，缩短持锁时间
                    latest_prices_snapshot = dict(latest_prices)
                    latest_price_version = price_version["value"]
                with options_lock:
                    latest_options_snapshot = dict(latest_options)
                    latest_options_sig_snapshot = dict(latest_options_sig)
                    latest_hover_sig_snapshot = dict(latest_hover_sig)
                    latest_options_version = options_version["value"]
                try:
                    need_redraw = False  # 仅在图上元素实际变化时触发重绘
                    # 期权版本无变化时，跳过整段散点更新逻辑
                    if latest_options_version != last_handled_options_version["value"]:
                        for port_index, port in enumerate(ports):
                            for row_index, stock_code in enumerate(stock_codes):
                                key = _panel_key(port_index, stock_code)
                                if key not in latest_options_snapshot:
                                    continue
                                options = latest_options_snapshot.get(key, [])
                                plot_signature = latest_options_sig_snapshot.get(key)
                                hover_signature = latest_hover_sig_snapshot.get(key)
                                if plot_signature is None:
                                    plot_signature = _options_signature(options)
                                if hover_signature is None:
                                    hover_signature = _options_hover_signature(options)
                                state = plot_states.get(key)
                                if last_drawn_options.get(key) == plot_signature:
                                    # 仅悬停字段变化时，更新 tooltip 数据，不触发重绘
                                    if (
                                        state is not None
                                        and last_hover_options.get(key) != hover_signature
                                    ):
                                        state["point_counts"].clear()
                                        state["point_counts"].update(
                                            _point_counts_from_options(options)
                                        )
                                    last_hover_options[key] = hover_signature
                                    continue

                                ax = axs[row_index][port_index]
                                # 期权分布有变化：更新/创建散点
                                if not options:
                                    # 期权清空：清散点并移除基准线
                                    if state is not None:
                                        update_plot(ax, options, state)
                                        need_redraw = True
                                    line, text = base_lines.get(key, (None, None))
                                    if line is not None:
                                        line.remove()
                                        text.remove()
                                        need_redraw = True
                                    base_lines[key] = (None, None)
                                    last_drawn_prices.pop(key, None)
                                    last_drawn_options[key] = plot_signature
                                    last_hover_options[key] = hover_signature
                                    continue

                                if state is None:
                                    base_line, base_text, state = plot_chart(
                                        ax,
                                        options,
                                        stock_code,
                                        None,
                                        chart_title=_panel_title(stock_code, port),
                                        show_legend=not legend_drawn,
                                    )
                                    legend_drawn = True
                                    plot_states[key] = state
                                    base_lines[key] = (base_line, base_text)
                                    need_redraw = True
                                else:
                                    update_plot(ax, options, state)
                                    need_redraw = True
                                last_drawn_options[key] = plot_signature
                                last_hover_options[key] = hover_signature

                                # 如果已拿到价格但还没画基准线，则补画
                                line, text = base_lines.get(key, (None, None))
                                if line is None and stock_code in latest_prices_snapshot:
                                    y_vals = [opt["strike_price"] for opt in options]
                                    line, text = draw_base_line(
                                        ax, y_vals, latest_prices_snapshot[stock_code]
                                    )
                                    base_lines[key] = (line, text)
                                    last_drawn_prices[key] = latest_prices_snapshot[stock_code]
                                    need_redraw = True
                        last_handled_options_version["value"] = latest_options_version

                    # 价格版本无变化时，跳过红线刷新逻辑
                    if latest_price_version != last_handled_price_version["value"]:
                        for port_index, port in enumerate(ports):
                            for row_index, stock_code in enumerate(stock_codes):
                                key = _panel_key(port_index, stock_code)
                                line, text = base_lines.get(key, (None, None))
                                if line is None or stock_code not in latest_prices_snapshot:
                                    # 无基准线，或拉取线程还没拿到该标的首次价格
                                    continue

                                last_drawn_price = last_drawn_prices.get(key)
                                latest_price = latest_prices_snapshot[stock_code]

                                # 最后拉取的价格和上次画图的价格对比，如果几乎没变就不移动基准线
                                if (
                                    last_drawn_price is not None
                                    and round(last_drawn_price, 2) == round(latest_price, 2)
                                ):
                                    logger.debug(
                                        f"chart {stock_code}@{port} price unchanged at y={latest_price}, skip"
                                    )
                                    continue

                                moved = move_base_line(
                                    axs[row_index][port_index], line, text, latest_price
                                )
                                if moved:
                                    last_drawn_prices[key] = latest_price
                                    need_redraw = True
                                    logger.info(
                                        f"chart {stock_code}@{port} moved base line to y={latest_price}"
                                    )
                        last_handled_price_version["value"] = latest_price_version

                    # 重绘
                    if need_redraw:
                        fig.canvas.draw_idle()
                except Exception as e:
                    logger.error(f"update error: {e}")
            # UI刷新时间（秒 -> 毫秒）
            timer = fig.canvas.new_timer(interval=ui_interval * 1000) 
            timer.add_callback(on_timer)
            timer.start()

            # 进入事件循环（交互式后端下保持 UI 响应）
            plt.show()
    except Exception as e:
        logger.error(f"error in futu api: {e}")
        sys.exit(1)


 
