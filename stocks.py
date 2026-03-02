import logging
import time
from datetime import datetime

import pytz
from futu import RET_OK

logger = logging.getLogger(__name__)

tz = pytz.timezone('America/New_York')

'''
return stocks like this:
{
	'HK.TCH230830P310000': {
		'code': 'HK.TCH230830P310000',
		'price': 5.54,
		'open_price': 0.0,
		'change_rate': 0.0,
		'amplitude': 0.0,
		'turnover': 0.0,
		'total_market_val': nan,
		'pe': nan,
		'pe_ttm': nan,
		...
	}
}
'''
def get_stock_data(quote_ctx, stock_codes, pre_price_instead = False, after_price_instead = False):
    page_size = 300 # futu api level2 limit
    page_stock_codes = []
    stocks = {}
    while len(stock_codes) > 0:
        page_stock_codes.append(stock_codes.pop(0))
        if len(page_stock_codes) == page_size or len(stock_codes) == 0: # full pagination or last page
            while True: # insure this pagination request processing successfully
                ret_code, snapshots = quote_ctx.get_market_snapshot(page_stock_codes)
                if ret_code != RET_OK:
                    logger.error("get_market_snapshot of %s, ret_code: %s, error: %s" % (page_stock_codes, ret_code, snapshots))
                    time.sleep(1.5) # error before maybe because of futu api level2 limit, 20 snapshots requests per 30s
                    continue

                page_stock_codes.clear()
                for i, snapshot in snapshots.iterrows(): # here each row is a tuple 
                    if snapshot["wrt_valid"] == True: #TODO ignore warrent in HK market
                        continue
                    stock = {}
                    code = snapshot["code"]
                    stocks[code] = stock
                    buildStock(snapshot, stock, pre_price_instead, after_price_instead)
                break
    return stocks

def buildStock(snapshot, stock, pre_price_instead, after_price_instead):
    now = datetime.now(tz)
    today = datetime(now.year, now.month, now.day)
    stock["code"] = snapshot["code"] 
    price = snapshot["last_price"]
    amplitude = snapshot["amplitude"]
    pre_price = snapshot["pre_price"]
    pre_change_rate = snapshot["pre_change_rate"]
    pre_amplitude = snapshot["pre_amplitude"]
    pre_turnover = snapshot["pre_turnover"]
    after_price = snapshot["after_price"]
    after_change_rate = snapshot["after_change_rate"]
    after_amplitude = snapshot["after_amplitude"]
    after_turnover = snapshot["after_turnover"]
    if snapshot["option_valid"] == False:
        if pre_price_instead and pre_price > 0:
            price = pre_price
        elif after_price_instead and after_price > 0:
            price = after_price
    stock["price"] = price
    stock["open_price"] = snapshot["open_price"]
    prev_close_price = snapshot["prev_close_price"]
    amplitude = snapshot["amplitude"]
    turnover = snapshot["turnover"]
    change_rate = "N/A"
    if prev_close_price == 0:
        stock["change_rate"] = change_rate
        logger.warning("snapshot:%s prev_close_price:%s" % (snapshot["code"], prev_close_price))
    else:
        change_rate = (snapshot["last_price"] - prev_close_price) / prev_close_price * 100
        if pre_price_instead and pre_change_rate != "N/A":
                change_rate = pre_change_rate
                amplitude = pre_amplitude
                turnover = pre_turnover
        elif after_price_instead and after_change_rate != "N/A":
                change_rate = after_change_rate
                amplitude = after_amplitude
                turnover = after_turnover
        stock["change_rate"] = str(round(change_rate, 2))
    stock["amplitude"] = round(amplitude, 2)
    stock["turnover"] = round(turnover / 100000000, 4)
    stock["total_market_val"] = round(snapshot["total_market_val"] / 100000000, 2)
    stock["pe"] = snapshot["pe_ratio"]
    stock["pe_ttm"] = snapshot["pe_ttm_ratio"]
    stock["prev_close_price"] = prev_close_price
    if snapshot["option_valid"] == True: 
        stock["stock_owner"] = snapshot["stock_owner"]
        strike_price = stock["strike_price"] = snapshot["option_strike_price"]
        strike_time = stock["strike_time"] = snapshot["strike_time"]
        stock["bid_price"] = snapshot["bid_price"]
        stock["ask_price"] = snapshot["ask_price"]
        stock["open_interest"] = snapshot["option_open_interest"]
        contract_size = stock["contract_size"] = snapshot["option_contract_size"]
        stock["type"] = snapshot["option_type"]
        stock["iv"] = snapshot["option_implied_volatility"]   
        stock["oi"] = snapshot["option_open_interest"]
        stock["iv"] = snapshot["option_implied_volatility"]
        stock["premium"] = snapshot["option_premium"]
        stock["delta"] = snapshot["option_delta"]
        stock["gamma"] = snapshot["option_gamma"]
        stock["vega"] = snapshot["option_vega"]
        stock["theta"] = snapshot["option_theta"]
        stock["rho"] = snapshot["option_rho"]
        stock["volume"] = snapshot["volume"] 
        days = (datetime.strptime(strike_time, '%Y-%m-%d') - today).days + 1 
        apy = 0 # Annual Percentage Yield
        if days > 0:
            surplus_profit = stock["price"] * contract_size # no handing fee is included here
            apy = surplus_profit / days * 365 / (strike_price * contract_size ) * 100 
        stock["apy"] = apy

def get_relevent_stocks(quote_ctx, hold_options, hold_stocks):
    stock_codes = set()
    stock_codes.update(set([p["stock_code"] for p in hold_options]))
    stock_codes.update(set([s["code"] for s in hold_stocks])) 
    # TODO get_market_snapshot接口调用美股要付费，港股免费
    supported_stock_codes = {stock_code for stock_code in stock_codes if stock_code.startswith("HK")}
    stocks = None
    if supported_stock_codes:
        stocks = get_stock_data(quote_ctx, list(supported_stock_codes))
    return stocks

def print_relevant_stocks(stocks):
    # 打印持仓关联股票（直接持仓的股票和期权对应股票）的信息
    if not stocks:
        pass
    all_stocks = []
    for code, stock in stocks.items():
        all_stocks.append({"code":code, "change_rate":stock["change_rate"], "price":stock["price"], "amplitude":stock["amplitude"], "total_market_val":stock["total_market_val"], "turnover":stock["turnover"], "pe":stock["pe"], "pe_ttm":stock["pe_ttm"]})
    if all_stocks:
        all_stocks.sort(key = lambda x: (x["change_rate"]), reverse = True)
        print("{:<22} {:>10} {:>10} {:>10} {:>10} {:>10} {:>10} {:>10}".format("code", "change%", "amplitude%", "price", "turnover", "m_val", "pe", "pe_ttm"))
        for p in all_stocks:
            print("{:<22} {:>10} {:>10.2f} {:>10.2f} {:>10.4f} {:>10.2f} {:>10.2f} {:>10.2f}".format(p["code"], p["change_rate"], p["amplitude"], p["price"], p["turnover"], p["total_market_val"], p["pe"], p["pe_ttm"]))