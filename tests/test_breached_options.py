import unittest
from unittest.mock import Mock

from opdash_web import (BREACH_COLOR, HOLLOW_COLOR, SHORT_COLOR, _mark_breached_options,
                        _normalize_option, build_web_snapshot)


def option(code, count, pl_ratio=0.0):
    # code: US.BMY261030P59000 -> type and strike come from the caller, as in core
    return _normalize_option({
        'code': code, 'type': 'PUT' if 'P' in code[-7:] else 'CALL', 'count': count,
        'strike_date': code[6:12], 'strike_price': int(code[-5:]) / 1000,
        'pl_ratio': pl_ratio, 'pl_val': 1.0,
    })


class BreachedOptionTests(unittest.TestCase):
    def test_short_put_above_price_and_short_call_below_price_are_breached(self):
        options = [option('US.BMY261030P59000', -1), option('US.BMY261218P55000', -1),
                   option('US.BMY261218C56000', -2), option('US.BMY270115C70000', -1)]
        self.assertEqual(_mark_breached_options(options, 57.32), 2)
        self.assertEqual([o['breached'] for o in options], [True, False, True, False])
        put, _, call, _ = options
        self.assertAlmostEqual(put['breach_pct'], (59 - 57.32) / 59 * 100)
        self.assertAlmostEqual(call['breach_pct'], (57.32 - 56) / 56 * 100)
        for breached in (put, call):
            self.assertEqual(breached['marker_line_color'], BREACH_COLOR)
            self.assertEqual(breached['marker_fill_color'], BREACH_COLOR)
        self.assertTrue(put['hover_text'].endswith('<br>ITM 2.85% (stock=57.32)'))

    def test_untouched_option_keeps_its_marker_and_hover(self):
        untouched = option('US.BMY261218P55000', -1)
        before = dict(untouched)
        _mark_breached_options([untouched], 57.32)
        self.assertEqual(untouched, {**before, 'breached': False})
        self.assertEqual(untouched['marker_line_color'], SHORT_COLOR)
        self.assertEqual(untouched['marker_fill_color'], HOLLOW_COLOR)

    def test_long_in_the_money_is_not_flagged(self):
        options = [option('US.BMY261030P59000', 1), option('US.BMY261218C56000', 3)]
        self.assertEqual(_mark_breached_options(options, 57.32), 0)
        self.assertEqual([o['breached'] for o in options], [False, False])

    def test_price_exactly_at_strike_or_missing_is_not_breached(self):
        options = [option('US.BMY261030P59000', -1), option('US.BMY261218C59000', -1)]
        self.assertEqual(_mark_breached_options(options, 59.0), 0)
        self.assertEqual(_mark_breached_options(options, None), 0)

    def test_breach_overrides_profit_fill(self):
        breached = option('US.BMY261218C56000', -1, pl_ratio=95.0)
        self.assertEqual(breached['marker_fill_color'], SHORT_COLOR)
        _mark_breached_options([breached], 57.32)
        self.assertEqual(breached['marker_fill_color'], BREACH_COLOR)

    def test_snapshot_reports_breached_count_per_panel(self):
        raw = lambda code, kind, strike: {'code': code, 'type': kind, 'count': -1,
                                          'strike_date': '261030', 'strike_price': strike}
        backend = Mock(ports=[11111], price_mode='auto')
        backend.get_connection_status.return_value = {'state': 'ready', 'errors': []}
        backend.get_state_snapshot.return_value = {
            'stock_codes': ['US.BMY', 'US.GDX'], 'stock_codes_version': 1,
            'prices': {'US.BMY': 57.32}, 'price_changes': {}, 'price_done_at': None,
            'options': {(0, 'US.BMY'): [raw('US.BMY261030P59000', 'PUT', 59.0),
                                       raw('US.BMY261030C70000', 'CALL', 70.0)],
                        (0, 'US.GDX'): [raw('US.GDX261030P90000', 'PUT', 90.0)]},
            'options_done_at_by_port': {}, 'options_version': 1, 'price_version': 1,
        }
        panels = {panel['stock_code']: panel for panel in build_web_snapshot(backend, 5)['panels']}
        self.assertEqual(panels['US.BMY']['breached_count'], 1)
        self.assertEqual([o['breached'] for o in panels['US.BMY']['options']], [True, False])
        # No price yet for GDX: nothing can be judged, so nothing is flagged.
        self.assertEqual(panels['US.GDX']['breached_count'], 0)


if __name__ == '__main__':
    unittest.main()
