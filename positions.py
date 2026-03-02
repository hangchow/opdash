import logging
import re
import time

logger = logging.getLogger(__name__)
from datetime import datetime, timedelta

import pytz
from futu import RET_OK, OptionType

from stocks import get_stock_data

tz = pytz.timezone('America/New_York')

def process_positions(quote_ctx, trade_ctx, pre_price_instead, after_price_instead):
    # 获取持仓信息
    positions = query_hold_positions(trade_ctx)
    hold_options = []
    hold_stocks = []
    total_today_pl_val = 0

    for i, position in positions.iterrows():
        code = position["code"]
        if not (code.startswith("US.") or code.startswith("HK.")):
            logger.warnning("ignore code:%s" % code)
            continue
        today_pl_val = round(position["today_pl_val"], 2)
        total_today_pl_val += today_pl_val
        count = position["qty"]
        if count == 0:  # ignore sold out
            continue
        market_val = position["market_val"]
        price = round(position["nominal_price"], 2)
        cost_price = round(position["cost_price"], 2)
        pl_ratio = round(position["pl_ratio"], 2)
        pl_val = round(position["pl_val"], 2)
        code_segs = re.split(r'([\d]\d*)', code)
        position = {"code": code, "count": count, "cost_price": cost_price, "price": price, "pl_ratio": pl_ratio, "pl_val": pl_val, "today_pl_val": today_pl_val, "market_val": market_val}
        if len(code_segs) > 3:  # is option
            hold_options.append(position)
        else:
            hold_stocks.append(position)

    position_info = {"today_gain": total_today_pl_val}
    hold_options_codes = set(p["code"] for p in hold_options)
    #hold_codes.update(s["code"] for s in hold_stocks)
    options_details = get_stock_data(quote_ctx, list(hold_options_codes), pre_price_instead, after_price_instead)

    for hold_option in hold_options:
        option_detail = options_details[hold_option["code"]]
        strike_price = option_detail["strike_price"]
        if option_detail["type"] == OptionType.PUT:
            hold_option["type"] = "PUT"
            stock_price = (strike_price - option_detail["price"]) / (1 - option_detail["premium"] / 100)
        else:
            hold_option["type"] = "CALL"
            stock_price = (strike_price + option_detail["price"]) / (1 + option_detail["premium"] / 100)
        open_interest = int(option_detail["open_interest"])
        volume = int(option_detail["volume"])
        iv = round(option_detail["iv"], 2)
        out = round((stock_price - strike_price) / stock_price * 100, 2) if stock_price > 0 else -1
        if (option_detail["type"] == OptionType.PUT and hold_option["count"] > 0) or (option_detail["type"] == OptionType.CALL and hold_option["count"] < 0):
            out = -out
        apy = -0.00 if hold_option["count"] > 0 else round(option_detail["apy"], 2)
        hold_option.update({
            "stock_code": option_detail["stock_owner"],
            "strike_time": option_detail["strike_time"],
            "contract_size": option_detail["contract_size"],
            "bid_price": option_detail["bid_price"],
            "ask_price": option_detail["ask_price"],
            "stock_price": stock_price,
            "change_rate": option_detail["change_rate"],
            "out": out,
            "strike_price": strike_price,
            "open_interest": open_interest,
            "volume": volume,
            "iv": iv,
            "apy": apy,
            "delta": option_detail["delta"],
            "gamma": option_detail["gamma"],
            "theta": option_detail["theta"],
            "vega": option_detail["vega"]
        })

    for s in hold_stocks: #todo
        s["bid_price"] = -1
        s["ask_price"] = -1


    return position_info, hold_options, hold_stocks

def query_hold_positions(trade_ctx, max_retries=999):
    retries = 0
    while retries < max_retries:
        ret_code, hold_all = trade_ctx.position_list_query(refresh_cache=True)
        if ret_code == RET_OK:
            return hold_all
        logger.error(f"position_list_query, ret_code: {ret_code}, error: {hold_all}")
        time.sleep(1.5)
        retries += 1
    raise Exception(f"Failed to get account info after {max_retries} retries")

def print_hold_options(hold_options):
    if not hold_options:
        print("No option positions")
        return
    
    header = f"{'code':<22}"\
        f"{'change%':>10}"\
        f"{'price':>10}"\
        f"{'ask_price':>10}"\
        f"{'bid_price':>10}"\
        f"{'c_price':>10}"\
        f"{'count':>10}"\
        f"{'m_val':>10}"\
        f"{'pl_ratio%':>10}"\
        f"{'pl_val':>10}"\
        f"{'t_pl_val':>12}"\
        f"{'s_date':>10}"\
        f"{'s_price':>10}"\
        f"{'r_price':>10}"\
        f"{'out%':>10}"\
        f"{'apy%':>10}"\
        f"{'oi':>10}"\
        f"{'volume':>10}"\
        f"{'iv%':>10}"\
        f"{'delta':>10}"\
        f"{'gamma':>10}"\
        f"{'theta':>10}"\
        f"{'vega':>10}"
    hold_options.sort(key=lambda x: (x["stock_code"], x["strike_time"], x["out"]))

    stock_option = None
    for p in hold_options:
        stock_code = p["stock_code"]
        if stock_code != stock_option:
            print(header)
            stock_option = stock_code
        if p['bid_price'] == 'N/A':
            p['bid_price'] = -1
        if p['ask_price'] == 'N/A':
            p['ask_price'] = -1
        print(
            f"{p['code']:<22}"
            f"{p['change_rate']:>10}"
            f"{p['price']:>10.2f}"
            f"{p['ask_price']:>10.2f}"
            f"{p['bid_price']:>10.2f}"
            f"{p['cost_price']:>10.2f}"
            f"{p['count']:>10}"
            f"{p['market_val']:>10.2f}"
            f"{p['pl_ratio']:>10.2f}"
            f"{p['pl_val']:>10.2f}"
            f"{p['today_pl_val']:>10.2f}"
            f"{p['strike_time']:>12}"
            f"{p['strike_price']:>10.2f}"
            f"{p['stock_price']:>10.2f}"
            f"{p['out']:>10.2f}"
            f"{p['apy']:>10.2f}"
            f"{p['open_interest']:>10}"
            f"{p['volume']:>10}"
            f"{p['iv']:>10.2f}"
            f"{p['delta']:>10.2f}"
            f"{p['gamma']:>10.2f}"
            f"{p['theta']:>10.2f}"
            f"{p['vega']:>10.2f}"
            ) 
    print(header) #为了看起来更方便

def print_hold_stocks(hold_stocks):
    if not hold_stocks:
        print("No stock positions")
        return
    
    print(
        f"{'code':<22}"
        f"{'change%':>10}"
        f"{'price':>10}"
        f"{'ask_price':>10}"
        f"{'bid_price':>10}"
        f"{'c_price':>10}"
        f"{'count':>10}"
        f"{'m_val':>10}"
        f"{'pl_ratio%':>10}"
        f"{'pl_val':>10}"
        f"{'t_pl_val':>10}"
        )
    hold_stocks.sort(key = lambda x: (x["market_val"]), reverse = True)
    for p in hold_stocks:
        print(
            f"{p['code']:<22}"
            f"{p['today_pl_val'] / (p['market_val'] - p['today_pl_val']) * 100.0:>10.2f}"
            f"{p['price']:>10.2f}"
            f"{p['ask_price']:>10.2f}"
            f"{p['bid_price']:>10.2f}"
            f"{p['cost_price']:>10.2f}"
            f"{p['count']:>10}"
            f"{p['market_val']:>10.2f}"
            f"{p['pl_ratio']:>10.2f}"
            f"{p['pl_val']:>10.2f}"
            f"{p['today_pl_val']:>10.2f}"
            )

