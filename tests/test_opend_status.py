import asyncio
from contextlib import contextmanager, nullcontext
import json
import logging
from threading import Event
import unittest
from unittest.mock import Mock, patch

from backend import OptionDashboardBackend
from opend_status import OpenDStatus, error_message
from opdash_web import create_app
from core import get_options_map


def make_backend():
    return OptionDashboardBackend(
        stock_codes=[], host='192.168.10.1', ports=[11111],
        poll_interval=10, price_interval=10, price_mode='auto', trade_market_filter=None,
        safe_trade_ctx=lambda *a, **k: nullcontext(Mock()),
        safe_quote_ctx=lambda *a, **k: nullcontext(Mock()),
        query_positions_with_log=Mock(return_value=[]), get_options_map=Mock(return_value={}),
        get_option_quotes_batch=Mock(return_value={}), merge_option_quotes=Mock(),
        get_stock_prices_with_fallback=Mock(return_value=({}, {})),
        get_stock_share_delta_map=Mock(return_value={}), get_options_delta_sum=lambda x: 0,
        discover_stock_codes=lambda positions: positions,
        options_signature=tuple, options_hover_signature=tuple,
        panel_key=lambda index, code: (index, code), pick_price_option_code=Mock(return_value=None),
    )


class OpenDStatusTests(unittest.TestCase):
    def test_empty_account_initializes_without_rejecting_auto_discovery(self):
        backend = make_backend()
        backend.get_options_map = get_options_map
        try:
            backend.start()
            self.assertTrue(backend.get_readiness()['ok'])
            self.assertTrue(backend.get_readiness()['empty'])
        finally:
            backend.stop()

    def test_initial_discovery_uses_union_before_building_each_port(self):
        backend = make_backend()
        backend.ports = [11111, 11112]
        backend.port_count = 2
        backend.query_positions_with_log.side_effect = [['US.AAPL'], ['US.TSLA']]
        with patch('backend.Thread') as thread:
            thread.return_value.is_alive.return_value = False
            backend.start()
            self.assertEqual(backend.stock_codes, ['US.AAPL', 'US.TSLA'])
            self.assertEqual(backend.query_positions_with_log.call_count, 2)
            for call in backend.get_options_map.call_args_list:
                self.assertEqual(call.args[1], ['US.AAPL', 'US.TSLA'])
            backend.stop()

    def test_sdk_reason_visible_before_constructor_returns(self):
        status = OpenDStatus('192.168.10.1', [11111])
        status.capture_logs()
        self.addCleanup(status.detach)
        with status.operation('connection', 11111):
            logging.getLogger('FTFileLog').warning('init connect fail: check sha error!')
            self.assertIn('check sha error', status.errors()[0]['message'])
            self.assertIn('--rsa_private_key', status.errors()[0]['hint'])

    def test_one_port_success_does_not_clear_another_port_failure(self):
        status = OpenDStatus('host', [11111, 11112])
        status.report('positions', 11111, 'must be encrypted')
        with status.operation('positions', 11112):
            pass
        self.assertEqual(len(status.errors()), 1)
        with status.operation('positions', 11111):
            pass
        self.assertEqual(status.errors(), [])

    def test_retry_timeout_preserves_concrete_sdk_cause(self):
        status = OpenDStatus('host', [11111])
        with self.assertRaises(RuntimeError):
            with status.operation('positions', 11111):
                status.report('positions', 11111, 'cross-network trade connections must be encrypted')
                raise RuntimeError('retry timeout')
        self.assertIn('must be encrypted', status.errors()[0]['message'])

    def test_secrets_redacted_and_message_bounded(self):
        text = error_message('password=secret token=abc -----BEGIN RSA PRIVATE KEY-----\nsecret\n-----END RSA PRIVATE KEY-----')
        self.assertNotIn('secret', text)
        self.assertNotIn('abc', text)
        self.assertEqual(len(error_message('x' * 5000)), 1200)

    def test_poll_failure_keeps_old_data_until_same_poll_recovers(self):
        backend = make_backend()
        backend.trade_ctxs[11111] = Mock()
        backend.quote_ctxs[11111] = Mock()
        backend.trade_locks[11111] = None
        backend.quote_locks[11111] = None
        backend.latest_options = {(0, 'US.AAPL'): ['old data']}
        backend.options_done_at_by_port = {11111: 'old timestamp'}
        backend.query_positions_with_log.side_effect = RuntimeError('cross-network trade connections must be encrypted')
        with patch.object(backend.stop_event, 'wait', side_effect=lambda interval: backend.stop_event.set()):
            backend._poll_options_by_port(0, 11111, 1)
        self.assertEqual(backend.latest_options[(0, 'US.AAPL')], ['old data'])
        self.assertEqual(backend.options_done_at_by_port[11111], 'old timestamp')
        self.assertEqual(backend.get_connection_status()['state'], 'error')
        backend.stop_event.clear()
        backend.query_positions_with_log.side_effect = None
        with patch.object(backend.stop_event, 'wait', side_effect=lambda interval: backend.stop_event.set()):
            backend._poll_options_by_port(0, 11111, 1)
        self.assertEqual(backend.status.errors(), [])

    def test_http_lifespan_serves_snapshot_during_blocked_handshake_and_recovers(self):
        backend = make_backend()
        entered, release = Event(), Event()

        @contextmanager
        def blocked_connect(*args, **kwargs):
            logging.getLogger('FTFileLog').warning('init connect fail: check sha error!')
            entered.set()
            release.wait(5)
            yield Mock()

        backend.safe_trade_ctx = blocked_connect
        app = create_app(backend, 5, manage_backend=True)
        routes = {route.path: route for route in app.routes}

        async def scenario():
            async with app.router.lifespan_context(app):
                try:
                    for _ in range(100):
                        if entered.is_set():
                            break
                        await asyncio.sleep(.01)
                    self.assertTrue(entered.is_set())
                    self.assertEqual(routes['/'].endpoint().status_code, 200)
                    self.assertEqual(routes['/healthz'].endpoint()['ok'], True)
                    self.assertEqual(routes['/readyz'].endpoint().status_code, 503)
                    snapshot = json.loads(routes['/api/snapshot'].endpoint().body)
                    self.assertEqual(snapshot['opend']['state'], 'error')
                    self.assertIn('check sha error', snapshot['opend']['errors'][0]['message'])
                finally:
                    release.set()
                for _ in range(100):
                    if backend.get_readiness()['ok']:
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(backend.get_connection_status()['state'], 'ready')
                self.assertEqual(routes['/readyz'].endpoint().status_code, 200)

        asyncio.run(scenario())

    def test_missing_key_is_visible_without_starting_backend(self):
        backend = make_backend()
        app = create_app(backend, 5, manage_backend=True, rsa_private_key='/missing/key.pem')

        async def scenario():
            async with app.router.lifespan_context(app):
                for _ in range(100):
                    if backend.status.errors():
                        break
                    await asyncio.sleep(.01)
                self.assertEqual(backend.get_connection_status()['state'], 'error')
                self.assertEqual(backend.status.errors()[0]['scope'], 'configuration')
                backend.query_positions_with_log.assert_not_called()

        asyncio.run(scenario())


if __name__ == '__main__':
    unittest.main()
