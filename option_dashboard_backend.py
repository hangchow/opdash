import logging
import math
from datetime import datetime, timezone
from contextlib import ExitStack
from threading import Event, Lock, Thread


class OptionDashboardBackend:
    """Shared polling backend used by both desktop and web entrypoints."""

    def __init__(
        self,
        *,
        stock_codes,
        host,
        ports,
        poll_interval,
        price_interval,
        price_mode,
        safe_trade_ctx,
        safe_quote_ctx,
        query_positions_with_log,
        get_options_map,
        get_option_quotes_batch,
        merge_option_quotes,
        get_stock_prices_with_fallback,
        get_stock_share_delta_map,
        get_options_delta_sum,
        options_signature,
        options_hover_signature,
        panel_key,
        pick_price_option_code,
        logger=None,
        init_purpose_prefix="init",
        poll_purpose_prefix="poll_options",
        price_thread_name="poll_price",
        options_thread_name_prefix="poll_options_",
        short_alert_threshold=None,
        short_alert_handler=None,
    ):
        self.stock_codes = list(stock_codes)
        self.host = host
        self.ports = list(ports)
        self.poll_interval = poll_interval
        self.price_interval = price_interval
        self.price_mode = price_mode
        self.port_count = len(self.ports)

        # Injected callables from existing implementation.
        self.safe_trade_ctx = safe_trade_ctx
        self.safe_quote_ctx = safe_quote_ctx
        self.query_positions_with_log = query_positions_with_log
        self.get_options_map = get_options_map
        self.get_option_quotes_batch = get_option_quotes_batch
        self.merge_option_quotes = merge_option_quotes
        self.get_stock_prices_with_fallback = get_stock_prices_with_fallback
        self.get_stock_share_delta_map = get_stock_share_delta_map
        self.get_options_delta_sum = get_options_delta_sum
        self.options_signature = options_signature
        self.options_hover_signature = options_hover_signature
        self.panel_key = panel_key
        self.pick_price_option_code = pick_price_option_code

        self.logger = logger or logging.getLogger("option_dashboard_backend")
        self.init_purpose_prefix = init_purpose_prefix
        self.poll_purpose_prefix = poll_purpose_prefix
        self.price_thread_name = price_thread_name
        self.options_thread_name_prefix = options_thread_name_prefix
        self.short_alert_threshold = (
            None if short_alert_threshold is None else float(short_alert_threshold)
        )
        self.short_alert_handler = short_alert_handler

        self.stop_event = Event()
        self.price_lock = Lock()
        self.options_lock = Lock()
        self.version_lock = Lock()
        self.alert_lock = Lock()

        self.trade_ctxs = {}
        self.quote_ctxs = {}
        self.trade_locks = {}
        self.quote_locks = {}
        self.workers = []
        self.exit_stack = None

        self.latest_prices = {}
        self.latest_options = {}
        self.latest_options_sig = {}
        self.latest_hover_sig = {}
        self.latest_option_code = {}
        self.latest_price_option_code = {}
        self.latest_delta_sum_by_panel = {}
        self.options_done_at_by_port = {}
        self.short_alert_hits_by_port = {port: set() for port in self.ports}
        self.price_done_at = None
        self.options_version = 0
        self.price_version = 0

    @staticmethod
    def _safe_float(value):
        try:
            number = float(value)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(number):
            return None
        return number

    @staticmethod
    def _is_short_option(option):
        side = str(option.get("side") or "").upper()
        if side == "SHORT":
            return True
        if side == "LONG":
            return False
        count = option.get("count")
        try:
            return float(count) < 0
        except (TypeError, ValueError):
            return False

    def _collect_new_short_close_alerts(self, port, options_snapshot):
        if self.short_alert_threshold is None:
            return []
        if not callable(self.short_alert_handler):
            return []
        threshold = self.short_alert_threshold
        current_hits = {}
        for stock_code, options in options_snapshot.items():
            for option in options:
                if not self._is_short_option(option):
                    continue
                option_code = option.get("code")
                if not option_code:
                    continue
                pl_ratio = self._safe_float(option.get("pl_ratio"))
                if pl_ratio is None or pl_ratio < threshold:
                    continue
                current_hits[(stock_code, option_code)] = {
                    "stock_code": stock_code,
                    "option": option,
                    "pl_ratio": pl_ratio,
                }
        with self.alert_lock:
            previous_hits = self.short_alert_hits_by_port.get(port, set())
            current_keys = set(current_hits.keys())
            new_keys = current_keys - previous_hits
            self.short_alert_hits_by_port[port] = current_keys
        return [current_hits[key] for key in sorted(new_keys)]

    def _emit_short_close_alerts(self, port, alerts):
        if not alerts or not callable(self.short_alert_handler):
            return
        for alert in alerts:
            option = alert["option"]
            try:
                self.short_alert_handler(
                    port=port,
                    stock_code=alert["stock_code"],
                    option_code=option.get("code"),
                    count=option.get("count"),
                    pl_ratio=alert["pl_ratio"],
                    threshold=self.short_alert_threshold,
                )
            except Exception as e:
                self.logger.error(
                    "short close alert handler failed: port=%s stock=%s option=%s err=%s",
                    port,
                    alert["stock_code"],
                    option.get("code"),
                    e,
                )

    def start(self):
        self.stop_event.clear()
        self.exit_stack = ExitStack()
        with self.alert_lock:
            self.short_alert_hits_by_port = {port: set() for port in self.ports}
        try:
            for port in self.ports:
                self.trade_ctxs[port] = self.exit_stack.enter_context(
                    self.safe_trade_ctx(self.host, port)
                )
                self.quote_ctxs[port] = self.exit_stack.enter_context(
                    self.safe_quote_ctx(self.host, port)
                )
                self.trade_locks[port] = Lock()
                self.quote_locks[port] = Lock()

            initial_options_by_panel = {}
            initial_option_code_by_panel = {}
            initial_plot_signatures = {}
            initial_hover_signatures = {}
            initial_price_option_codes = {}
            initial_delta_sum_by_panel = {}

            for port_index, port in enumerate(self.ports):
                trade_ctx = self.trade_ctxs[port]
                quote_ctx = self.quote_ctxs[port]
                trade_lock = self.trade_locks[port]
                quote_lock = self.quote_locks[port]

                positions_snapshot = self.query_positions_with_log(
                    trade_ctx,
                    trade_lock,
                    purpose=f"{self.init_purpose_prefix}:{port}",
                )
                options_snapshot = self.get_options_map(
                    trade_ctx,
                    self.stock_codes,
                    positions=positions_snapshot,
                )
                startup_short_alerts = self._collect_new_short_close_alerts(
                    port, options_snapshot
                )
                self._emit_short_close_alerts(port, startup_short_alerts)

                option_quotes = self.get_option_quotes_batch(
                    quote_ctx,
                    [
                        option["code"]
                        for options in options_snapshot.values()
                        for option in options
                    ],
                    quote_lock=quote_lock,
                )
                for options in options_snapshot.values():
                    self.merge_option_quotes(options, option_quotes)
                stock_share_delta_map = self.get_stock_share_delta_map(
                    positions_snapshot,
                    self.stock_codes,
                )

                for stock_code in self.stock_codes:
                    key = self.panel_key(port_index, stock_code)
                    options = options_snapshot.get(stock_code, [])
                    option_code = options[0]["code"] if options else None
                    initial_options_by_panel[key] = options
                    initial_option_code_by_panel[key] = option_code
                    initial_plot_signatures[key] = self.options_signature(options)
                    initial_hover_signatures[key] = self.options_hover_signature(options)
                    initial_delta_sum_by_panel[key] = (
                        stock_share_delta_map.get(stock_code, 0.0)
                        + self.get_options_delta_sum(options)
                    )
                    if stock_code not in initial_price_option_codes and option_code:
                        initial_price_option_codes[stock_code] = option_code
                self.options_done_at_by_port[port] = datetime.now(timezone.utc).isoformat()

            price_source_port = self.ports[0]
            price_quote_ctx = self.quote_ctxs[price_source_port]
            price_quote_lock = self.quote_locks[price_source_port]
            initial_prices = self.get_stock_prices_with_fallback(
                price_quote_ctx,
                self.stock_codes,
                initial_price_option_codes,
                price_mode=self.price_mode,
                quote_lock=price_quote_lock,
            )
            initial_price_done_at = (
                datetime.now(timezone.utc).isoformat() if initial_prices else None
            )

            with self.price_lock:
                self.latest_prices = dict(initial_prices)
                self.price_done_at = initial_price_done_at
            with self.options_lock:
                self.latest_options = dict(initial_options_by_panel)
                self.latest_option_code = dict(initial_option_code_by_panel)
                self.latest_options_sig = dict(initial_plot_signatures)
                self.latest_hover_sig = dict(initial_hover_signatures)
                self.latest_delta_sum_by_panel = dict(initial_delta_sum_by_panel)
                for stock_code in self.stock_codes:
                    self.latest_price_option_code[stock_code] = self.pick_price_option_code(
                        stock_code,
                        self.latest_option_code,
                        self.port_count,
                    )

            t = Thread(
                target=self._poll_price_all,
                args=(price_source_port, self.price_interval),
                daemon=True,
                name=self.price_thread_name,
            )
            t.start()
            self.workers.append(t)

            for port_index, port in enumerate(self.ports):
                t = Thread(
                    target=self._poll_options_by_port,
                    args=(port_index, port, self.poll_interval),
                    daemon=True,
                    name=f"{self.options_thread_name_prefix}{port}",
                )
                t.start()
                self.workers.append(t)

            self.logger.info(
                "Backend polling started: stocks=%s, ports=%s, mode=%s",
                ",".join(self.stock_codes),
                self.ports,
                self.price_mode,
            )
        except Exception:
            self.stop()
            raise

    def stop(self):
        self.stop_event.set()
        for t in self.workers:
            t.join(timeout=1.0)
        self.workers = []

        if self.exit_stack is not None:
            try:
                self.exit_stack.close()
            except Exception as e:
                self.logger.warning("Failed to close contexts cleanly: %s", e)
            self.exit_stack = None

    def get_state_snapshot(self):
        with self.price_lock:
            prices_snapshot = dict(self.latest_prices)
            price_done_at_snapshot = self.price_done_at
        with self.options_lock:
            options_snapshot = {
                key: list(options)
                for key, options in self.latest_options.items()
            }
            options_sig_snapshot = dict(self.latest_options_sig)
            hover_sig_snapshot = dict(self.latest_hover_sig)
            delta_sum_by_panel_snapshot = dict(self.latest_delta_sum_by_panel)
            options_done_at_by_port_snapshot = dict(self.options_done_at_by_port)
        with self.version_lock:
            options_version = self.options_version
            price_version = self.price_version
        return {
            "prices": prices_snapshot,
            "price_done_at": price_done_at_snapshot,
            "options": options_snapshot,
            "options_sig": options_sig_snapshot,
            "hover_sig": hover_sig_snapshot,
            "delta_sum_by_panel": delta_sum_by_panel_snapshot,
            "options_done_at_by_port": options_done_at_by_port_snapshot,
            "options_version": options_version,
            "price_version": price_version,
        }

    def _poll_price_all(self, price_source_port, interval):
        quote_ctx = self.quote_ctxs[price_source_port]
        quote_lock = self.quote_locks[price_source_port]

        while not self.stop_event.is_set():
            try:
                with self.options_lock:
                    option_code_snapshot = {
                        stock_code: option_code
                        for stock_code, option_code in self.latest_price_option_code.items()
                        if option_code
                    }
                prices = self.get_stock_prices_with_fallback(
                    quote_ctx,
                    self.stock_codes,
                    option_code_snapshot,
                    price_mode=self.price_mode,
                    quote_lock=quote_lock,
                )
                if prices:
                    price_done_at = datetime.now(timezone.utc).isoformat()
                    with self.price_lock:
                        self.latest_prices.update(prices)
                        self.price_done_at = price_done_at
                    with self.version_lock:
                        self.price_version += 1
            except Exception as e:
                self.logger.error("poll price error: %s", e)
            finally:
                self.stop_event.wait(interval)

    def _poll_options_by_port(self, port_index, port, interval):
        trade_ctx = self.trade_ctxs[port]
        quote_ctx = self.quote_ctxs[port]
        trade_lock = self.trade_locks[port]
        quote_lock = self.quote_locks[port]

        while not self.stop_event.is_set():
            try:
                positions_snapshot = self.query_positions_with_log(
                    trade_ctx,
                    trade_lock,
                    purpose=f"{self.poll_purpose_prefix}:{port}",
                )
                options_snapshot = self.get_options_map(
                    trade_ctx,
                    self.stock_codes,
                    positions=positions_snapshot,
                )
                new_short_alerts = self._collect_new_short_close_alerts(
                    port, options_snapshot
                )
                self._emit_short_close_alerts(port, new_short_alerts)
                option_code_snapshot = {
                    stock_code: (options[0]["code"] if options else None)
                    for stock_code, options in options_snapshot.items()
                }

                option_quotes = self.get_option_quotes_batch(
                    quote_ctx,
                    [
                        option["code"]
                        for options in options_snapshot.values()
                        for option in options
                    ],
                    quote_lock=quote_lock,
                )
                for options in options_snapshot.values():
                    self.merge_option_quotes(options, option_quotes)
                stock_share_delta_map = self.get_stock_share_delta_map(
                    positions_snapshot,
                    self.stock_codes,
                )

                options_sig_snapshot = {
                    stock_code: self.options_signature(options)
                    for stock_code, options in options_snapshot.items()
                }
                hover_sig_snapshot = {
                    stock_code: self.options_hover_signature(options)
                    for stock_code, options in options_snapshot.items()
                }

                with self.options_lock:
                    for stock_code in self.stock_codes:
                        key = self.panel_key(port_index, stock_code)
                        options = options_snapshot.get(stock_code, [])
                        self.latest_options[key] = options
                        self.latest_option_code[key] = option_code_snapshot.get(stock_code)
                        self.latest_options_sig[key] = options_sig_snapshot.get(stock_code, ())
                        self.latest_hover_sig[key] = hover_sig_snapshot.get(stock_code, ())
                        self.latest_delta_sum_by_panel[key] = (
                            stock_share_delta_map.get(stock_code, 0.0)
                            + self.get_options_delta_sum(options)
                        )

                    for stock_code in self.stock_codes:
                        self.latest_price_option_code[stock_code] = self.pick_price_option_code(
                            stock_code,
                            self.latest_option_code,
                            self.port_count,
                        )
                    self.options_done_at_by_port[port] = datetime.now(timezone.utc).isoformat()

                with self.version_lock:
                    self.options_version += 1
            except Exception as e:
                self.logger.error("poll options error on port %s: %s", port, e)
            finally:
                self.stop_event.wait(interval)
