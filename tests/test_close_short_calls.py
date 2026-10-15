import contextlib
import io
import logging
from threading import Event
import time
import unittest
from unittest.mock import Mock

import pandas as pd
from futu import RET_ERROR, RET_OK, TimeInForce, TrdEnv, TrdSide

from close_short_calls import (
    close_short_calls,
    connect_or_fail,
    normalize_underlying,
    plan_orders,
    resolve_real_acc_id,
    short_call_quantities,
    working_buy_quantities,
)

ACC_ID = 1234567890
CALL_A = "US.AAPL270115C250000"
CALL_B = "US.AAPL270115C300000"


def positions(*rows):
    return pd.DataFrame(rows, columns=["code", "qty"])


def orders(*rows):
    return pd.DataFrame(rows, columns=["order_id", "code", "trd_side", "qty", "dealt_qty", "price", "order_status"])


def make_trade_ctx(position_rows, today_rows=(), history_rows=()):
    ctx = Mock()
    ctx.get_acc_list.return_value = (RET_OK, pd.DataFrame(
        [{"acc_id": ACC_ID, "trd_env": TrdEnv.REAL}, {"acc_id": 42, "trd_env": TrdEnv.SIMULATE}]
    ))
    ctx.position_list_query.return_value = (RET_OK, positions(*position_rows))
    ctx.order_list_query.return_value = (RET_OK, orders(*today_rows))
    ctx.history_order_list_query.return_value = (RET_OK, orders(*history_rows))
    ctx.place_order.side_effect = lambda **kwargs: (RET_OK, pd.DataFrame([{"order_id": f"new-{kwargs['code']}"}]))
    return ctx


def make_quote_ctx(bids):
    ctx = Mock()
    ctx.get_market_snapshot.side_effect = lambda codes: (
        RET_OK, pd.DataFrame({"code": codes, "bid_price": [bids[code] for code in codes]})
    )
    return ctx


def run(trade_ctx, quote_ctx, **kwargs):
    with contextlib.redirect_stdout(io.StringIO()):
        return close_short_calls(trade_ctx, quote_ctx, "US.AAPL", settle_seconds=0, **kwargs)


class ParsingTests(unittest.TestCase):
    def test_normalize_underlying_defaults_to_us(self):
        self.assertEqual(normalize_underlying("aapl"), "US.AAPL")
        self.assertEqual(normalize_underlying("HK.TCH"), "HK.TCH")
        self.assertEqual(normalize_underlying("hk.700"), "HK.00700")

    def test_short_calls_ignore_puts_longs_closed_and_other_underlyings(self):
        result = short_call_quantities(positions(
            (CALL_A, -2),
            (CALL_B, 1),
            ("US.AAPL270115P200000", -1),
            ("US.AAPL270115C350000", 0),
            ("US.AAPLX270115C250000", -1),
            ("HK.AAPL270115C250000", -1),
            ("US.AAPL", 100),
        ), "US.AAPL")
        self.assertEqual(result, {CALL_A: 2})

    def test_working_buys_count_unfilled_part_once_and_skip_final_or_sell_orders(self):
        rows = [
            ("1", CALL_A, TrdSide.BUY_BACK, 3, 1, 1.0, "FILLED_PART"),
            ("1", CALL_A, TrdSide.BUY_BACK, 3, 1, 1.0, "FILLED_PART"),
            ("2", CALL_A, TrdSide.BUY, 1, 0, 1.0, "SUBMITTED"),
            ("3", CALL_A, TrdSide.BUY_BACK, 1, 0, 1.0, "CANCELLED_ALL"),
            ("4", CALL_A, TrdSide.BUY_BACK, 1, 1, 1.0, "FILLED_ALL"),
            ("5", CALL_A, TrdSide.SELL_SHORT, 1, 0, 1.0, "SUBMITTED"),
            ("6", "US.AAPL270115P200000", TrdSide.BUY_BACK, 1, 0, 1.0, "SUBMITTED"),
        ]
        self.assertEqual(working_buy_quantities(orders(*rows), "US.AAPL"), {CALL_A: 3})

    def test_plan_only_orders_the_uncovered_contracts(self):
        self.assertEqual(plan_orders({CALL_A: 3, CALL_B: 1}, {CALL_A: 1, CALL_B: 2}), {CALL_A: 2})


class AccountTests(unittest.TestCase):
    def test_single_real_account_is_picked(self):
        self.assertEqual(resolve_real_acc_id(make_trade_ctx([])), ACC_ID)

    def test_unknown_requested_account_is_rejected(self):
        with self.assertRaises(RuntimeError):
            resolve_real_acc_id(make_trade_ctx([]), 42)

    def test_several_real_accounts_need_acc_id(self):
        ctx = make_trade_ctx([])
        ctx.get_acc_list.return_value = (RET_OK, pd.DataFrame(
            [{"acc_id": ACC_ID, "trd_env": TrdEnv.REAL}, {"acc_id": 7, "trd_env": TrdEnv.REAL}]
        ))
        with self.assertRaises(RuntimeError):
            resolve_real_acc_id(ctx)
        self.assertEqual(resolve_real_acc_id(ctx, 7), 7)


class CloseShortCallsTests(unittest.TestCase):
    def test_places_missing_quantity_at_the_bid(self):
        trade_ctx = make_trade_ctx(
            [(CALL_A, -2), (CALL_B, -1)],
            today_rows=[("1", CALL_A, TrdSide.BUY_BACK, 1, 0, 1.5, "SUBMITTED")],
        )
        self.assertTrue(run(trade_ctx, make_quote_ctx({CALL_A: 1.55, CALL_B: 0.8})))
        calls = {call.kwargs["code"]: call.kwargs for call in trade_ctx.place_order.call_args_list}
        self.assertEqual(set(calls), {CALL_A, CALL_B})
        self.assertEqual((calls[CALL_A]["qty"], calls[CALL_A]["price"]), (1, 1.55))
        self.assertEqual((calls[CALL_B]["qty"], calls[CALL_B]["price"]), (1, 0.8))
        for kwargs in calls.values():
            self.assertEqual(kwargs["trd_side"], TrdSide.BUY)
            self.assertEqual(kwargs["trd_env"], TrdEnv.REAL)
            self.assertEqual(kwargs["acc_id"], ACC_ID)
            self.assertEqual(kwargs["time_in_force"], TimeInForce.DAY)

    def test_gtc_order_from_an_earlier_day_counts_as_covered(self):
        trade_ctx = make_trade_ctx(
            [(CALL_A, -1)],
            history_rows=[("9", CALL_A, TrdSide.BUY_BACK, 1, 0, 1.2, "SUBMITTED")],
        )
        self.assertTrue(run(trade_ctx, make_quote_ctx({})))
        trade_ctx.place_order.assert_not_called()

    def test_dry_run_and_missing_bid_place_nothing(self):
        trade_ctx = make_trade_ctx([(CALL_A, -1), (CALL_B, -1)])
        self.assertTrue(run(trade_ctx, make_quote_ctx({CALL_A: 1.0, CALL_B: 1.0}), dry_run=True))
        self.assertTrue(run(trade_ctx, make_quote_ctx({CALL_A: 0.0, CALL_B: float("nan")})))
        trade_ctx.place_order.assert_not_called()

    def test_rejected_order_reports_failure(self):
        trade_ctx = make_trade_ctx([(CALL_A, -1)])
        trade_ctx.place_order.side_effect = None
        trade_ctx.place_order.return_value = (RET_ERROR, "Trade is not unlocked. Please unlock trade first.")
        self.assertFalse(run(trade_ctx, make_quote_ctx({CALL_A: 1.0})))

    def test_unlock_uses_the_prompted_password_and_stops_on_failure(self):
        trade_ctx = make_trade_ctx([(CALL_A, -1)])
        trade_ctx.unlock_trade.return_value = (RET_ERROR, "Wrong trading password")
        with self.assertRaises(RuntimeError):
            run(trade_ctx, make_quote_ctx({CALL_A: 1.0}), unlock=True, prompt=lambda _: "secret")
        trade_ctx.unlock_trade.assert_called_once_with("secret")
        trade_ctx.place_order.assert_not_called()


class ConnectTests(unittest.TestCase):
    def test_first_sdk_connect_failure_ends_the_wait_with_a_hint(self):
        never = Event()

        def factory():
            logging.getLogger("FTFileLog").warning(
                "[open_context_base.py:408] _init_connect_sync: init connect fail: conn=0(1) msg=check sha error!"
            )
            never.wait(5)

        started = time.monotonic()
        try:
            with self.assertRaises(RuntimeError) as raised:
                with connect_or_fail(factory, "127.0.0.1:11111", timeout=5):
                    self.fail("connected")
        finally:
            never.set()
        self.assertLess(time.monotonic() - started, 2)
        self.assertIn("check sha error", str(raised.exception))
        self.assertIn("--rsa_private_key", str(raised.exception))

    def test_ready_context_is_yielded_and_closed(self):
        ctx = Mock()
        with connect_or_fail(lambda: ctx, "127.0.0.1:11111", timeout=5) as connected:
            self.assertIs(connected, ctx)
        ctx.close.assert_called_once_with()


if __name__ == "__main__":
    unittest.main()
