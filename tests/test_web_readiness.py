from datetime import datetime, timedelta, timezone
from threading import Event, Lock
import unittest
from unittest.mock import Mock, patch

from backend import OptionDashboardBackend
from core import resolve_stock_codes, TrdMarket
from opdash_web import create_app


class ReadinessTests(unittest.TestCase):
    def setUp(self):
        self.backend = OptionDashboardBackend.__new__(OptionDashboardBackend)
        b = self.backend
        b.options_lock = Lock()
        b.price_lock = Lock()
        b.stop_event = Event()
        b.stock_codes = ["US.AAPL", "US.TSLA"]
        b.ports = [11111]
        b.poll_interval = b.price_interval = 10
        self.now = datetime.now(timezone.utc).isoformat()
        b.options_done_at_by_port = {11111: self.now}
        b.price_success_at = {code: self.now for code in b.stock_codes}

    def test_each_underlying_requires_fresh_success(self):
        self.assertTrue(self.backend.get_readiness()["ok"])
        self.backend.price_success_at["US.TSLA"] = (datetime.now(timezone.utc) - timedelta(minutes=5)).isoformat()
        self.assertFalse(self.backend.get_readiness()["ok"])

    def test_successful_empty_account_is_ready(self):
        self.backend.stock_codes = []
        self.backend.price_success_at = {}
        self.assertTrue(self.backend.get_readiness()["ok"])
        self.backend.options_done_at_by_port = {}
        self.assertFalse(self.backend.get_readiness()["ok"])

    def test_stopped_backend_is_not_ready(self):
        self.backend.stop_event.set()
        self.assertFalse(self.backend.get_readiness()["ok"])

    def test_empty_auto_start_uses_all_markets(self):
        with patch("core.discover_option_stock_codes", return_value=[]):
            codes, market, auto = resolve_stock_codes(None, "127.0.0.1", [11111], allow_empty_auto=True)
            self.assertEqual(codes, [])
            self.assertEqual(market, TrdMarket.NONE)
            self.assertTrue(auto)
            with self.assertRaises(ValueError):
                resolve_stock_codes(None, "127.0.0.1", [11111])

    def test_empty_poll_clears_panels_after_all_ports_seen(self):
        b = self.backend
        b.discover_stock_codes = lambda positions: positions
        b.discovered_codes_by_port = {11111: list(b.stock_codes)}
        b.port_count = 1
        b.version_lock = Lock()
        b.stock_codes_version = 0
        b.logger = Mock()
        b._prune_panel_state = Mock()
        self.assertTrue(b._refresh_auto_stock_codes(11111, []))
        self.assertEqual(b.stock_codes, [])
        self.assertEqual(b.stock_codes_version, 1)

    def test_http_readiness_status_and_asset_version(self):
        app = create_app(self.backend, 5)
        routes = {route.path: route for route in app.routes}
        self.assertEqual(routes["/readyz"].endpoint().status_code, 200)
        self.backend.stop_event.set()
        self.assertEqual(routes["/readyz"].endpoint().status_code, 503)
        body = routes["/"].endpoint().body.decode()
        self.assertNotIn("__ASSET_VERSION__", body)
        self.assertIn("/static/vendor/plotly-2.35.2.min.js", body)


if __name__ == "__main__":
    unittest.main()
