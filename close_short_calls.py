"""Buy to close every short call on one underlying at the bid, without duplicating working orders."""
import argparse
from contextlib import contextmanager
import datetime as dt
import getpass
import logging
import os
import sys
from threading import Event, Thread
import time

import pandas as pd
from futu import (RET_OK, OpenQuoteContext, OpenSecTradeContext, OrderStatus, OrderType, SecurityFirm,
                  SysConfig, TimeInForce, TrdAccStatus, TrdEnv, TrdSide)

from core import (
    HK_NUMERIC_STOCK_CODE_RE,
    _parse_option_code_fields,
    _safe_float,
    bind_parser_error_handler,
    configure_futu_encryption,
    infer_trade_market_filter,
    normalize_stock_code,
)
from opend_status import error_hint, error_message
from options import OptionEnum

logger = logging.getLogger("close_short_calls")

# 终态订单不会再成交；其余状态（含部分成交、撤单中）都按仍在工作计入已挂数量，宁可少补不重复挂。
# 已失效（DISABLED）的港股单还能重新生效，所以不算终态
FINAL_ORDER_STATUSES = {
    OrderStatus.SUBMIT_FAILED,
    OrderStatus.FILLED_ALL,
    OrderStatus.CANCELLED_PART,
    OrderStatus.CANCELLED_ALL,
    OrderStatus.FAILED,
    OrderStatus.DELETED,
    OrderStatus.FILL_CANCELLED,
}
BUY_SIDES = {TrdSide.BUY, TrdSide.BUY_BACK}  # 服务器把买入平空回报为 BUY_BACK
# 当日订单查不到更早挂出的单：GTC 最多存续 90 个自然日，GTD 存续到指定日期且未查到上限，往回查一年
ORDER_LOOKBACK_DAYS = 366
SETTLE_SECONDS = 3.0     # 下单后等这么久再回查订单状态
CONNECT_TIMEOUT = 30.0   # 等 OpenD 连接就绪的上限秒数
SDK_CONNECT_FAILURE = "connect fail"   # SDK 每次连接失败（含 init connect fail）时日志里的关键字
SDK_LOGGERS = ("FTFileLog", "FTConsoleLog")   # 任一日志被调高级别时，另一个仍能收到失败日志
CONNECT_FALLBACK_HINT = "请检查 OpenD 是否运行并已登录，以及 --host / --port 是否正确。"


def normalize_underlying(raw_code):
    code = normalize_stock_code(raw_code)
    if code and "." not in code:
        code = f"US.{code}"
    return code


def is_call_on(option_code, underlying):
    parsed = _parse_option_code_fields(option_code)
    return bool(parsed) and parsed["type"] == OptionEnum.CALL and parsed["stock_code_hint"] == underlying


def short_call_quantities(positions, underlying):
    """Option code -> contracts short, for calls on the underlying."""
    result = {}
    if positions is None or positions.empty:
        return result
    for _, row in positions.iterrows():
        qty = int(round(_safe_float(row.get("qty"), 0.0)))
        if qty < 0 and is_call_on(row.get("code"), underlying):
            result[row["code"]] = result.get(row["code"], 0) - qty
    return result


def working_buy_quantities(orders, underlying):
    """Option code -> contracts still unfilled on working buy orders for calls on the underlying."""
    result = {}
    if orders is None or orders.empty:
        return result
    for _, row in orders.drop_duplicates("order_id").iterrows():
        if row.get("order_status") in FINAL_ORDER_STATUSES or row.get("trd_side") not in BUY_SIDES:
            continue
        if not is_call_on(row.get("code"), underlying):
            continue
        remaining = int(round(_safe_float(row.get("qty"), 0.0) - _safe_float(row.get("dealt_qty"), 0.0)))
        if remaining > 0:
            result[row["code"]] = result.get(row["code"], 0) + remaining
    return result


def plan_orders(short_calls, working):
    """Option code -> contracts still to buy once working orders are counted."""
    return {
        code: short - working.get(code, 0)
        for code, short in sorted(short_calls.items())
        if short > working.get(code, 0)
    }


def mask_acc_id(acc_id):
    return f"****{str(acc_id)[-4:]}"


def resolve_real_acc_id(trade_ctx, requested=None):
    ret, accounts = trade_ctx.get_acc_list()
    if ret != RET_OK:
        raise RuntimeError(f"get_acc_list failed: {accounts}")
    usable = accounts["trd_env"] == TrdEnv.REAL
    if "acc_status" in accounts:
        usable &= accounts["acc_status"] != TrdAccStatus.DISABLED   # 迁移到综合账户后留下的停用旧账户
    real_ids = [int(acc_id) for acc_id in accounts.loc[usable, "acc_id"]]
    if requested is not None:
        if requested not in real_ids:
            raise RuntimeError(f"--acc_id {mask_acc_id(requested)} is not a real account on this OpenD")
        return requested
    if len(real_ids) != 1:
        raise RuntimeError(f"{len(real_ids)} real accounts on this OpenD; choose one with --acc_id")
    return real_ids[0]


def query_orders(trade_ctx, acc_id):
    ret, todays = trade_ctx.order_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
    if ret != RET_OK:
        raise RuntimeError(f"order_list_query failed: {todays}")
    start = dt.date.today() - dt.timedelta(days=ORDER_LOOKBACK_DAYS)
    ret, history = trade_ctx.history_order_list_query(
        trd_env=TrdEnv.REAL, acc_id=acc_id, start=start.strftime("%Y-%m-%d 00:00:00")
    )
    if ret != RET_OK:
        raise RuntimeError(f"history_order_list_query failed: {history}")
    # 同一订单可能同时出现在当日和历史查询里，保留排在前面的当日记录
    return pd.concat([todays, history], ignore_index=True).drop_duplicates("order_id")


class _ConnectFailure(logging.Handler):
    def __init__(self, event):
        super().__init__(logging.WARNING)
        self.event = event
        self.message = None

    def emit(self, record):
        message = record.getMessage()
        if self.message is None and SDK_CONNECT_FAILURE in message.lower():
            self.message = message
            self.event.set()


@contextmanager
def connect_or_fail(factory, description, timeout=CONNECT_TIMEOUT):
    """Build a futu context, raising on the first failed connect.

    The SDK constructor retries every 6 seconds and never returns while OpenD rejects the
    connection (for example without the RSA key), so it runs on a daemon thread and the
    SDK's own failure log ends the wait.
    """
    done = Event()
    failure = _ConnectFailure(done)
    result = {}

    def build():
        try:
            result["ctx"] = factory()
        except Exception as error:
            result["error"] = error
        finally:
            done.set()

    sdk_loggers = [logging.getLogger(name) for name in SDK_LOGGERS]
    for sdk_logger in sdk_loggers:
        sdk_logger.addHandler(failure)
    try:
        Thread(target=build, name=f"connect {description}", daemon=True).start()
        done.wait(timeout)
    finally:
        for sdk_logger in sdk_loggers:
            sdk_logger.removeHandler(failure)
    ctx = result.get("ctx")
    if ctx is None:
        # 失败后放弃构造线程：它是守护线程，单次脚本随进程退出
        reason = failure.message or result.get("error") or f"not ready after {timeout:g}s"
        message = error_message(reason)
        hint = error_hint(message)
        if hint == error_hint(""):   # 通用兜底提示写的是看板会自动重试，脚本不会
            hint = CONNECT_FALLBACK_HINT
        raise RuntimeError(f"cannot connect to OpenD {description}: {message}\n{hint}")
    try:
        yield ctx
    finally:
        try:
            ctx.close()
        except Exception as error:
            logger.warning("Failed to close futu context cleanly: %s", error)


def close_short_calls(trade_ctx, quote_ctx, underlying, *, acc_id=None, unlock=False, dry_run=False,
                      prompt=getpass.getpass, settle_seconds=SETTLE_SECONDS):
    """Place the missing buy-to-close orders.

    Returns True when every short call ends up on a working or filled close order
    (or, with dry_run, would be ordered).
    """
    if unlock:
        ret, data = trade_ctx.unlock_trade(prompt("Futu trade password: "))
        if ret != RET_OK:
            raise RuntimeError(f"unlock_trade failed: {data}")
        print("Trading unlocked")
    acc_id = resolve_real_acc_id(trade_ctx, acc_id)
    print(f"Account {mask_acc_id(acc_id)}")

    # 先查订单再查持仓：两次查询之间成交的平仓单仍按工作中计入、持仓已减少，只会少补；
    # 反过来查会把它同时算作仍需平仓和已不在工作，重复下单
    orders = query_orders(trade_ctx, acc_id)
    ret, positions = trade_ctx.position_list_query(trd_env=TrdEnv.REAL, acc_id=acc_id, refresh_cache=True)
    if ret != RET_OK:
        raise RuntimeError(f"position_list_query failed: {positions}")
    short_calls = short_call_quantities(positions, underlying)
    if not short_calls:
        print(f"No short calls on {underlying}")
        return True

    working = working_buy_quantities(orders, underlying)
    plan = plan_orders(short_calls, working)
    for code, short in sorted(short_calls.items()):
        print(f"{code}: short {short}, working buy {working.get(code, 0)}, to place {plan.get(code, 0)}")
    if not plan:
        print("Every short call already has working close orders; nothing placed")
        return True

    ret, snapshot = quote_ctx.get_market_snapshot(list(plan))
    if ret != RET_OK:
        raise RuntimeError(f"get_market_snapshot failed: {snapshot}")
    bids = dict(zip(snapshot["code"], snapshot["bid_price"]))
    ok = True
    placed = []
    for code, qty in plan.items():
        bid = _safe_float(bids.get(code), 0.0)
        if bid <= 0:
            ok = False
            print(f"{code}: no bid, skipped")
            continue
        if dry_run:
            print(f"[dry run] {code}: buy {qty} @ {bid:g}")
            continue
        ret, data = trade_ctx.place_order(
            price=bid, qty=qty, code=code, trd_side=TrdSide.BUY, order_type=OrderType.NORMAL,
            trd_env=TrdEnv.REAL, acc_id=acc_id, time_in_force=TimeInForce.DAY,
        )
        if ret == RET_OK:
            placed.append(data.iloc[0]["order_id"])
            print(f"{code}: buy {qty} @ {bid:g} -> order {placed[-1]}")
        else:
            ok = False
            hint = " (run with --unlock)" if "unlock" in str(data).lower() else ""
            print(f"{code}: buy {qty} @ {bid:g} failed: {data}{hint}")

    if placed:
        ok = report_placed_orders(trade_ctx, acc_id, placed, settle_seconds) and ok
    return ok


def report_placed_orders(trade_ctx, acc_id, order_ids, settle_seconds):
    """Print the new orders' status; False when the broker has already rejected or cancelled one."""
    time.sleep(settle_seconds)
    try:
        orders = query_orders(trade_ctx, acc_id)
    except RuntimeError as error:
        # 订单已经发出，回查失败只影响展示，不把成功的下单报成失败
        print(f"Orders were placed, but their status could not be read: {error}")
        return True
    orders = orders[orders["order_id"].isin(order_ids)]
    print("Placed orders:")
    print(orders[["order_id", "code", "qty", "dealt_qty", "price", "order_status"]].to_string(index=False))
    dead = orders[orders["order_status"].isin(FINAL_ORDER_STATUSES - {OrderStatus.FILLED_ALL})
                  | (orders["order_status"] == OrderStatus.DISABLED)]
    for _, row in dead.iterrows():
        print(f"{row['code']}: order {row['order_id']} is {row['order_status']}")
    return dead.empty


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Buy to close every short call on one underlying at the bid; "
                    "quantities already on working buy orders are not ordered again."
    )
    parser.add_argument(
        "stock_code",
        help="underlying as it appears in option codes, e.g. US.AAPL, AAPL (US by default), HK.TCH",
    )
    parser.add_argument("--host", metavar="", default="127.0.0.1", help="futu server host (default: 127.0.0.1)")
    parser.add_argument("--port", metavar="", type=int, default=11111, help="futu server port (default: 11111)")
    parser.add_argument(
        "--rsa_private_key",
        metavar="",
        default=os.environ.get("FUTU_RSA_PRIVATE_KEY"),
        help="RSA private key shared with OpenD; enables encryption (or set FUTU_RSA_PRIVATE_KEY)",
    )
    parser.add_argument(
        "--acc_id", metavar="", type=int, default=None,
        help="real trading account id; needed only when OpenD has more than one",
    )
    parser.add_argument("--unlock", action="store_true", help="prompt for the trade password and unlock trading first")
    parser.add_argument("--dry_run", action="store_true", help="show the orders without placing them")
    bind_parser_error_handler(parser)
    args = parser.parse_args(argv)
    args.stock_code = normalize_underlying(args.stock_code)
    if not args.stock_code:
        parser.error("No valid stock code provided. Example: US.AAPL or HK.TCH")
    if HK_NUMERIC_STOCK_CODE_RE.fullmatch(args.stock_code):
        # 港股期权代码用期权类别代码（腾讯是 HK.TCH），按数字代码永远匹配不到，会误报没有空头 call
        parser.error(f"{args.stock_code}: HK options use the option class code (HK.TCH for HK.00700); pass that code")
    try:
        configure_futu_encryption(args.rsa_private_key)
    except ValueError as error:
        parser.error(str(error))
    return args


def main(argv=None):
    logging.basicConfig(level=logging.WARNING, format="%(asctime)s - %(levelname)-5s - %(message)s")
    args = parse_args(argv)
    SysConfig.set_all_thread_daemon(True)
    endpoint = f"{args.host}:{args.port}"
    try:
        with connect_or_fail(lambda: OpenSecTradeContext(
                filter_trdmarket=infer_trade_market_filter([args.stock_code]), host=args.host, port=args.port,
                security_firm=SecurityFirm.FUTUSECURITIES), endpoint) as trade_ctx, \
                connect_or_fail(lambda: OpenQuoteContext(host=args.host, port=args.port), endpoint) as quote_ctx:
            ok = close_short_calls(trade_ctx, quote_ctx, args.stock_code, acc_id=args.acc_id,
                                   unlock=args.unlock, dry_run=args.dry_run)
    except RuntimeError as error:
        print(f"Error: {error}", file=sys.stderr)
        return 1
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
