from contextlib import nullcontext
import unittest
from unittest.mock import Mock, patch

import pandas as pd
from futu import OptionType

import core
from backend import OptionDashboardBackend
from core import (RET_OK, _get_stock_prices_from_snapshot_batch,
                  _get_stock_prices_with_fallback, _group_option_positions_by_stock_codes,
                  _stock_snapshot_code)

SNAPSHOT_ROWS = {
    'HK.00700': {'code': 'HK.00700', 'last_price': 426.0, 'prev_close_price': 420.0},
    'US.BMY': {'code': 'US.BMY', 'last_price': 57.0, 'prev_close_price': 57.5},
}


def make_quote_ctx():
    # OpenD rejects the whole page when any code is not a real security.
    def snapshot(codes):
        unknown = [code for code in codes if code not in SNAPSHOT_ROWS]
        if unknown:
            return -1, 'Unknown stock. ' + unknown[0].split('.', 1)[1]
        return RET_OK, pd.DataFrame([SNAPSHOT_ROWS[code] for code in codes])
    return Mock(get_market_snapshot=Mock(side_effect=snapshot))


class StockSnapshotCodeTests(unittest.TestCase):
    def setUp(self):
        core._unknown_stock_until.clear()
        self.addCleanup(core._unknown_stock_until.clear)

    def test_owner_replaces_option_class_code(self):
        self.assertEqual(_stock_snapshot_code('HK.TCH', 'HK.00700'), 'HK.00700')
        self.assertEqual(_stock_snapshot_code('HK.TCH', 'HK.700'), 'HK.00700')
        self.assertEqual(_stock_snapshot_code('US.BMY', 'US.BMY'), 'US.BMY')

    def test_unresolved_hk_class_code_is_not_queried(self):
        self.assertIsNone(_stock_snapshot_code('HK.TCH'))
        self.assertEqual(_stock_snapshot_code('HK.00700'), 'HK.00700')
        self.assertEqual(_stock_snapshot_code('US.BMY'), 'US.BMY')

    def test_prices_are_keyed_by_panel_code(self):
        quote_ctx = make_quote_ctx()
        prices, changes = _get_stock_prices_from_snapshot_batch(
            quote_ctx, ['HK.TCH', 'US.BMY'], price_mode='last',
            stock_owner_map={'HK.TCH': 'HK.00700'},
        )
        quote_ctx.get_market_snapshot.assert_called_once_with(['HK.00700', 'US.BMY'])
        self.assertEqual(prices, {'HK.TCH': 426.0, 'US.BMY': 57.0})
        self.assertAlmostEqual(changes['HK.TCH']['change_val'], 6.0)
        self.assertAlmostEqual(changes['US.BMY']['change_val'], -0.5)

    def test_class_code_and_its_stock_share_one_query(self):
        quote_ctx = make_quote_ctx()
        prices, _ = _get_stock_prices_from_snapshot_batch(
            quote_ctx, ['HK.TCH', 'HK.00700'], price_mode='last',
            stock_owner_map={'HK.TCH': 'HK.00700'},
        )
        quote_ctx.get_market_snapshot.assert_called_once_with(['HK.00700'])
        self.assertEqual(prices, {'HK.TCH': 426.0, 'HK.00700': 426.0})

    def test_unresolved_class_code_does_not_fail_other_stocks(self):
        quote_ctx = make_quote_ctx()
        with patch('core._get_stock_prices_from_options_batch',
                   return_value={'HK.TCH': 425.8}) as implied:
            prices, changes = _get_stock_prices_with_fallback(
                quote_ctx, ['HK.TCH', 'US.BMY'], {'HK.TCH': 'HK.TCH261127C460000'},
                price_mode='last',
            )
        quote_ctx.get_market_snapshot.assert_called_once_with(['US.BMY'])
        self.assertEqual(implied.call_args.args[1], {'HK.TCH': 'HK.TCH261127C460000'})
        self.assertEqual(prices, {'HK.TCH': 425.8, 'US.BMY': 57.0})
        self.assertEqual(list(changes), ['US.BMY'])


    def test_unknown_code_is_dropped_and_skipped_without_failing_page(self):
        quote_ctx = make_quote_ctx()
        prices, _ = _get_stock_prices_from_snapshot_batch(
            quote_ctx, ['US.SPXW', 'US.BMY'], price_mode='last',
        )
        self.assertEqual(prices, {'US.BMY': 57.0})
        self.assertEqual([c.args[0] for c in quote_ctx.get_market_snapshot.call_args_list],
                         [['US.SPXW', 'US.BMY'], ['US.BMY']])

        # Later rounds skip the unknown code instead of paying for a rejected page.
        quote_ctx.get_market_snapshot.reset_mock()
        _get_stock_prices_from_snapshot_batch(quote_ctx, ['US.SPXW', 'US.BMY'], price_mode='last')
        quote_ctx.get_market_snapshot.assert_called_once_with(['US.BMY'])

        # The skip expires, so a code that becomes valid is quoted again.
        with patch('core.time.monotonic', return_value=core.time.monotonic() + core.UNKNOWN_STOCK_COOLDOWN + 1):
            quote_ctx.get_market_snapshot.reset_mock()
            _get_stock_prices_from_snapshot_batch(quote_ctx, ['US.SPXW', 'US.BMY'], price_mode='last')
        self.assertEqual(quote_ctx.get_market_snapshot.call_args_list[0].args[0], ['US.SPXW', 'US.BMY'])

    def test_other_rejections_are_not_retried(self):
        quote_ctx = Mock(get_market_snapshot=Mock(return_value=(-1, 'Request too frequent')))
        prices, _ = _get_stock_prices_from_snapshot_batch(quote_ctx, ['US.BMY'], price_mode='last')
        self.assertEqual(prices, {})
        quote_ctx.get_market_snapshot.assert_called_once_with(['US.BMY'])
        self.assertEqual(core._unknown_stock_until, {})


class CrossMarketGroupingTests(unittest.TestCase):
    def test_same_symbol_in_two_markets_stays_in_its_own_panel(self):
        # Meituan's HK option class code MET and MetLife's US ticker MET share the bare alias.
        meituan = {'code': 'HK.MET261127C150000', 'stock_owner': 'HK.03690',
                   'stock_code_hint': 'HK.MET', 'type': OptionType.CALL,
                   'strike_date': '2026-11-27', 'strike_price': 150.0}
        metlife = {'code': 'US.MET261120C80000', 'stock_owner': 'US.MET',
                   'stock_code_hint': 'US.MET', 'type': OptionType.CALL,
                   'strike_date': '2026-11-20', 'strike_price': 80.0}
        options_map = _group_option_positions_by_stock_codes([meituan, metlife], ['HK.MET', 'US.MET'])
        self.assertEqual({code: [o['code'] for o in options] for code, options in options_map.items()},
                         {'HK.MET': [meituan['code']], 'US.MET': [metlife['code']]})
        self.assertEqual(OptionDashboardBackend._stock_owners(options_map),
                         {'HK.MET': 'HK.03690', 'US.MET': 'US.MET'})

    def test_class_code_still_groups_under_its_hk_panel(self):
        tencent = {'code': 'HK.TCH261127C460000', 'stock_owner': 'HK.00700',
                   'stock_code_hint': 'HK.TCH', 'type': OptionType.CALL,
                   'strike_date': '2026-11-27', 'strike_price': 460.0}
        options_map = _group_option_positions_by_stock_codes([tencent], ['HK.TCH', 'US.BMY'])
        self.assertEqual([o['code'] for o in options_map['HK.TCH']], [tencent['code']])
        self.assertEqual(options_map['US.BMY'], [])


class BackendStockOwnerTests(unittest.TestCase):
    def make_backend(self, get_options_map, get_prices):
        backend = OptionDashboardBackend(
            stock_codes=['HK.TCH'], host='127.0.0.1', ports=[11111],
            poll_interval=10, price_interval=10, price_mode='auto', trade_market_filter=None,
            safe_trade_ctx=lambda *a, **k: nullcontext(Mock()),
            safe_quote_ctx=lambda *a, **k: nullcontext(Mock()),
            query_positions_with_log=Mock(return_value=[]), get_options_map=get_options_map,
            get_option_quotes_batch=Mock(return_value={}), merge_option_quotes=Mock(),
            get_stock_prices_with_fallback=get_prices,
            get_stock_share_delta_map=Mock(return_value={}), get_options_delta_sum=lambda x: 0,
            options_signature=tuple, options_hover_signature=tuple,
            panel_key=lambda index, code: (index, code),
            pick_price_option_code=Mock(return_value='HK.TCH261127C460000'),
        )
        for store in (backend.trade_ctxs, backend.quote_ctxs):
            store[11111] = Mock()
        backend.trade_locks[11111] = backend.quote_locks[11111] = None
        return backend

    def poll_once(self, backend, poll, *args):
        backend.stop_event.clear()
        with patch.object(backend.stop_event, 'wait',
                          side_effect=lambda interval: backend.stop_event.set()):
            poll(*args)

    def test_price_poll_uses_owner_and_keeps_it_through_a_missed_quote(self):
        option = {'code': 'HK.TCH261127C460000', 'stock_owner': 'HK.00700'}
        get_options_map = Mock(return_value={'HK.TCH': [option]})
        get_prices = Mock(return_value=({'HK.TCH': 426.0}, {}))
        backend = self.make_backend(get_options_map, get_prices)

        self.poll_once(backend, backend._poll_options_by_port, 0, 11111, 1)
        self.poll_once(backend, backend._poll_price_all, 11111, 1)
        self.assertEqual(get_prices.call_args.kwargs['stock_owner_map'], {'HK.TCH': 'HK.00700'})

        # A rate-limited option snapshot returns no stock_owner for that round.
        get_options_map.return_value = {'HK.TCH': [{'code': option['code'], 'stock_owner': None}]}
        self.poll_once(backend, backend._poll_options_by_port, 0, 11111, 1)
        self.poll_once(backend, backend._poll_price_all, 11111, 1)
        self.assertEqual(get_prices.call_args.kwargs['stock_owner_map'], {'HK.TCH': 'HK.00700'})

    def test_start_resolves_owner_before_first_price_query(self):
        option = {'code': 'HK.TCH261127C460000', 'stock_owner': 'HK.00700'}
        get_prices = Mock(return_value=({'HK.TCH': 426.0}, {}))
        backend = self.make_backend(Mock(return_value={'HK.TCH': [option]}), get_prices)
        with patch('backend.Thread'):
            backend.start()
        self.assertEqual(get_prices.call_args.kwargs['stock_owner_map'], {'HK.TCH': 'HK.00700'})
        self.assertEqual(backend.latest_stock_owner, {'HK.TCH': 'HK.00700'})

    def test_price_round_with_no_price_at_all_fails_immediately(self):
        # Every quote page was rejected: only logs, no exception, and nothing refreshed.
        def rejected(*args, **kwargs):
            core.logger.error('get_market_snapshot stock prices failed: -1, Unknown stock. TCH')
            return {}, {}
        backend = self.make_backend(Mock(return_value={}), Mock(side_effect=rejected))
        backend.status.capture_logs()
        self.addCleanup(backend.status.detach)
        self.poll_once(backend, backend._poll_price_all, 11111, 1)
        self.assertTrue(backend.status.failed())
        self.assertIn('Unknown stock', backend.status.errors()[0]['message'])

        # The next round that prices every underlying clears it.
        backend.get_stock_prices_with_fallback = Mock(return_value=({'HK.TCH': 426.0}, {}))
        self.poll_once(backend, backend._poll_price_all, 11111, 1)
        self.assertFalse(backend.status.failed())
        self.assertEqual(backend.status.errors(), [])

    def test_closed_position_drops_owner(self):
        backend = self.make_backend(Mock(return_value={}), Mock(return_value=({}, {})))
        backend.latest_stock_owner = {'HK.TCH': 'HK.00700', 'US.BMY': 'US.BMY'}
        backend._prune_panel_state(['US.BMY'])
        self.assertEqual(backend.latest_stock_owner, {'US.BMY': 'US.BMY'})


if __name__ == '__main__':
    unittest.main()
