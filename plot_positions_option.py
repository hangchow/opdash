import argparse
import datetime
import logging
import sys
import textwrap

import matplotlib.dates as mdates
import matplotlib.pyplot as plt
import mplcursors
import numpy as np
from matplotlib import transforms
from matplotlib.legend_handler import HandlerTuple
from matplotlib.lines import Line2D

from option_dashboard_backend import OptionDashboardBackend
from option_dashboard_core import (
    HOLLOW_FACE_COLOR,
    LONG_POSITION_COLOR,
    SHORT_POSITION_COLOR,
    SIDE_SHORT,
    add_dashboard_common_args,
    bind_parser_error_handler,
    build_dashboard_header_data,
    build_server_settings,
    format_option_position_count_text,
    format_server_settings_text,
    get_profit_highlight_threshold,
    get_option_position_counts,
    make_telegram_short_close_alert_handler,
    _fmt_int,
    _fmt_percent,
    _fmt_price,
    _get_option_quotes_batch,
    _get_stock_prices_with_fallback,
    _merge_option_quotes,
    _option_side,
    _options_hover_signature,
    _options_signature,
    _panel_key,
    _panel_title,
    _pick_price_option_code,
    _query_positions_with_log,
    _safe_float,
    get_options_map,
    get_options_delta_sum,
    get_options_short_value_sum,
    get_stock_share_delta_map,
    parse_ports_arg,
    safe_quote_ctx,
    safe_trade_ctx,
    set_profit_highlight_threshold,
)
from options import OptionEnum

logger = logging.getLogger("plot_positions_option")  # 固定日志名，避免显示为 __main__

# 本脚本不再依赖 futu.yaml，改用命令行指定 host/port
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(threadName)s - %(levelname)s - %(message)s"  # 日志以线程名为主，避免 __main__ 噪音
)
Y_RANGE_PAD_RATIO = 0.1
Y_RANGE_EDGE_TRIGGER_RATIO = 0.1
Y_RANGE_MIN_PAD = 1.0
BASE_PRICE_LABEL_YSHIFT_PTS = 10


def parse_args():
    parser = argparse.ArgumentParser(
        description="Parameter settings for runing %(prog)s" % {'prog': sys.argv[0]}
    )
    add_dashboard_common_args(parser)
    bind_parser_error_handler(parser)

    args = parser.parse_args()
    ports = parse_ports_arg(args.port, parser, logger_obj=logger, max_ports=2)
    return (
        args.stock_codes,
        args.host,
        ports,
        args.poll_interval,
        args.price_interval,
        args.ui_interval,
        args.price_mode,
        args.profit_highlight_threshold,
        args.telegram_bot_token,
        args.telegram_chat_id,
    )


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
            "profit_value": option.get("pl_val"),
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
    threshold = get_profit_highlight_threshold()
    for option in options:
        count = abs(int(option.get("count", 1)))
        size = 40 + max(0, count - 1) * 20
        strike_dt = datetime.datetime.strptime(option["strike_date"], "%y%m%d")
        pl_ratio = _safe_float(option.get("pl_ratio", 0), 0.0)
        profit_hit = pl_ratio >= threshold  # 仅高亮达标点
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


def _compute_panel_y_range(strike_prices, stock_price=None):
    candidates = []
    for value in strike_prices or []:
        num = _safe_float(value, None)
        if num is None:
            continue
        candidates.append(float(num))
    price_num = _safe_float(stock_price, None)
    if price_num is not None:
        candidates.append(float(price_num))
    if not candidates:
        return None
    y_min = min(candidates)
    y_max = max(candidates)
    y_pad = max(Y_RANGE_MIN_PAD, (y_max - y_min) * Y_RANGE_PAD_RATIO)
    return (y_min - y_pad, y_max + y_pad)


def _strike_bounds_key(strike_prices):
    finite_strikes = []
    for value in strike_prices or []:
        num = _safe_float(value, None)
        if num is None:
            continue
        finite_strikes.append(float(num))
    if not finite_strikes:
        return "none"
    return f"{min(finite_strikes):.6f}|{max(finite_strikes):.6f}"


def _apply_axis_y_range(ax, y_range):
    if y_range is None:
        return False
    curr_min, curr_max = ax.get_ylim()
    target_min, target_max = y_range
    if abs(curr_min - target_min) < 1e-9 and abs(curr_max - target_max) < 1e-9:
        return False
    ax.set_ylim(target_min, target_max)
    return True


def _is_price_near_or_outside_y_edge(ax, stock_price):
    price_num = _safe_float(stock_price, None)
    if price_num is None:
        return False
    curr_min, curr_max = ax.get_ylim()
    y_min, y_max = min(curr_min, curr_max), max(curr_min, curr_max)
    span = y_max - y_min
    if span <= 0:
        return True
    inner_min = y_min + span * Y_RANGE_EDGE_TRIGGER_RATIO
    inner_max = y_max - span * Y_RANGE_EDGE_TRIGGER_RATIO
    return price_num <= inner_min or price_num >= inner_max


def _maybe_expand_panel_y_range_for_price(ax, strike_prices, stock_price):
    if not _is_price_near_or_outside_y_edge(ax, stock_price):
        return False
    target_range = _compute_panel_y_range(strike_prices, stock_price)
    if target_range is None:
        return False
    curr_min, curr_max = ax.get_ylim()
    y_min, y_max = min(curr_min, curr_max), max(curr_min, curr_max)
    expanded_range = (
        min(y_min, target_range[0]),
        max(y_max, target_range[1]),
    )
    return _apply_axis_y_range(ax, expanded_range)


def _marker_legend_items():
    # Marker semantics legend shown once at figure level
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
    handles = [
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
    labels = [
        "Short Call",
        "Long Call",
        "Short Put",
        "Long Put",
        f"profit% >= {get_profit_highlight_threshold():.0f}%",
    ]
    return handles, labels


def _figure_axes_right_edge(fig):
    axes = [ax for ax in fig.axes if ax.get_visible()]
    if not axes:
        return 0.995
    return max(ax.get_position().x1 for ax in axes)


def _add_marker_legend(fig, anchor_y=0.992):
    handles, labels = _marker_legend_items()
    anchor_x = _figure_axes_right_edge(fig)
    fig.legend(
        handles=handles,
        labels=labels,
        loc="upper right",
        bbox_to_anchor=(anchor_x, anchor_y),
        bbox_transform=fig.transFigure,
        fontsize=9,
        framealpha=0.9,
        borderaxespad=0.0,
        ncol=len(labels),
        columnspacing=1.0,
        handletextpad=0.45,
        handler_map={tuple: HandlerTuple(ndivide=None)},
    )


def _panel_bottom_label(options):
    return format_option_position_count_text(get_option_position_counts(options))


def _draw_position_count_text(ax, options):
    return ax.text(
        0.995,
        0.015,
        _panel_bottom_label(options),
        transform=ax.transAxes,
        ha="right",
        va="bottom",
        fontsize=8.5,
        color="#334155",
        bbox={
            "facecolor": "white",
            "alpha": 0.82,
            "edgecolor": "#cbd5e1",
            "boxstyle": "round,pad=0.2",
        },
        clip_on=True,
        zorder=5,
    )


def _update_position_count_text(text_artist, options):
    if text_artist is None:
        return
    text_artist.set_text(_panel_bottom_label(options))


def plot_chart(
    ax,
    options,
    stock_code,
    stock_price=None,
    chart_title=None,
    show_y_label=True,
    y_ticks_on_right=False,
):
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
    ax.set_ylabel("Strike Price" if show_y_label else "")
    if y_ticks_on_right:
        ax.yaxis.set_label_position("right")
        ax.yaxis.tick_right()
    else:
        ax.yaxis.set_label_position("left")
        ax.yaxis.tick_left()
    ax.set_title(chart_title or f"{stock_code} Option Positions")
    _apply_axis_y_range(ax, _compute_panel_y_range(plot_data["y_all"], stock_price))

    state = {
        "call_sc": call_sc,
        "put_sc": put_sc,
        "point_counts": point_counts,
        "last_annotation": None,
        "cursor": None,
        "count_text": _draw_position_count_text(ax, options),
        "y_bounds_key": _strike_bounds_key(plot_data["y_all"]),
    }

    cursor = mplcursors.cursor([call_sc, put_sc], hover=True)
    state["cursor"] = cursor
    if hasattr(cursor, "enabled"):
        cursor.enabled = bool(plot_data["y_all"])

    @cursor.connect("add")
    def on_hover(sel):
        x_num = round(float(sel.target[0]), 8)
        y_val = round(float(sel.target[1]), 6)
        strike_dt = mdates.num2date(sel.target[0]).strftime("%Y-%m-%d")
        details = point_counts.get((x_num, y_val), [])
        if details:
            lines = [f"{strike_dt}, {sel.target[1]:.2f}"]
            for detail in sorted(
                details,
                key=lambda d: (
                    0 if d.get("side") == SIDE_SHORT else 1,
                    0 if d.get("type") == OptionEnum.CALL else 1,
                ),
            ):
                side_text = "SHORT" if detail.get("side") == SIDE_SHORT else "LONG"
                type_text = "CALL" if detail.get("type") == OptionEnum.CALL else "PUT"
                profit_value = _safe_float(detail.get("profit_value"), None)
                profit_value_text = "N/A" if profit_value is None else f"{profit_value:+.2f}"
                lines.append(
                    f"{side_text} {type_text}: "
                    f"count={detail['count']}, price={_fmt_price(detail.get('price'))}, "
                    f"bid_price={_fmt_price(detail.get('bid_price'))}, ask_price={_fmt_price(detail.get('ask_price'))}, "
                    f"volume={_fmt_int(detail.get('volume'))}, oi={_fmt_int(detail.get('open_interest'))}, "
                    f"profit%={_fmt_percent(detail.get('profit_ratio'))}, "
                    f"p/l={profit_value_text}"
                )
            sel.annotation.set_text("\n".join(lines))
        else:
            sel.annotation.set_text(f"{strike_dt}, {sel.target[1]:.2f}")
        sel.annotation.get_bbox_patch().set(fc="white", alpha=0.8)
        state["last_annotation"] = sel.annotation

    base_line, base_text = (None, None)
    if stock_price is not None:
        logger.info(f"chart {stock_code} init base line at y={stock_price}")
        base_line, base_text = draw_base_line(ax, plot_data["y_all"], stock_price)
    return base_line, base_text, state


def update_plot(ax, options, state, stock_price=None):
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
    if unique_dates:
        x_min_num = mdates.date2num(min(unique_dates))
        x_max_num = mdates.date2num(max(unique_dates))
        x_pad = max(1.0, (x_max_num - x_min_num) * 0.1)
        ax.set_xlim(
            mdates.num2date(x_min_num - x_pad),
            mdates.num2date(x_max_num + x_pad),
        )
    y_all = plot_data["y_all"]
    y_bounds_key = _strike_bounds_key(y_all)
    if y_all:
        target_range = _compute_panel_y_range(y_all, stock_price)
        if state.get("y_bounds_key") != y_bounds_key:
            _apply_axis_y_range(ax, target_range)
        else:
            _maybe_expand_panel_y_range_for_price(ax, y_all, stock_price)
    state["y_bounds_key"] = y_bounds_key
    _update_position_count_text(state.get("count_text"), options)


def draw_base_line(ax, y, price):
    # 绘制当前股价基准线
    label_x, label_ha = _base_price_label_anchor(ax)
    base_y = round(price, 2)
    line = ax.axhline(y=base_y, color='red', linestyle='--', linewidth=1)
    text = ax.text(
        label_x,
        base_y,
        f"{base_y:.2f}",
        color='red',
        fontsize=10,
        ha=label_ha,
        va='center',
        transform=_base_price_text_transform(ax),
        clip_on=True,
        bbox=dict(facecolor='white', edgecolor='none', pad=0.35),
    )
    return line, text


def _base_price_label_anchor(ax):
    on_right = ax.yaxis.get_label_position() == "right"
    if not on_right:
        on_right = ax.yaxis.get_ticks_position() == "right"
    if on_right:
        return 0.995, "right"
    return 0.005, "left"


def _base_price_text_transform(ax):
    return transforms.offset_copy(
        ax.get_yaxis_transform(),
        fig=ax.figure,
        y=BASE_PRICE_LABEL_YSHIFT_PTS,
        units='points',
    )


def move_base_line(ax, line, text, new_y):
    label_x, label_ha = _base_price_label_anchor(ax)
    new_y_round = round(new_y, 2)
    curr_y = line.get_ydata()[0]
    if round(curr_y, 2) == new_y_round:
        return False
    line.set_ydata([new_y_round, new_y_round])
    text.set_position((label_x, new_y_round))
    text.set_ha(label_ha)
    text.set_transform(_base_price_text_transform(ax))
    text.set_text(f"{new_y_round:.2f}")
    return True


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
                window.state("zoomed")
                return
            if hasattr(window, "showMaximized"):
                window.showMaximized()
                return
            if hasattr(window, "Maximize"):
                window.Maximize(True)
                return
        if hasattr(manager, "full_screen_toggle"):
            manager.full_screen_toggle()
    except Exception as e:
        logger.debug(f"maximize window skipped: {e}")


def _wrap_figure_text(text, width=180):
    return textwrap.fill(
        text,
        width=width,
        break_long_words=False,
        break_on_hyphens=False,
    )


def _apply_layout_with_header_footer(fig, header_title, header_status, footer_text):
    wrapped_status = _wrap_figure_text(header_status, width=180)
    wrapped_footer = _wrap_figure_text(footer_text, width=260)
    status_line_count = wrapped_status.count("\n") + 1
    footer_line_count = wrapped_footer.count("\n") + 1
    # Keep header/footer readable while maximizing chart area.
    top_margin = max(0.89, 0.962 - status_line_count * 0.018)
    bottom_margin = min(0.14, 0.018 + footer_line_count * 0.018)
    fig.tight_layout(
        rect=(0.005, bottom_margin, 0.995, top_margin),
        pad=0.5,
        h_pad=1.0,
    )
    fig.suptitle(
        header_title,
        x=0.01,
        y=0.995,
        ha="left",
        va="top",
        fontsize=16,
        fontweight="bold",
    )
    status_artist = fig.text(
        0.01,
        0.968,
        wrapped_status,
        transform=fig.transFigure,
        ha="left",
        va="top",
        fontsize=9,
        color="#475569",
    )
    fig.text(
        0.01,
        0.004,
        wrapped_footer,
        transform=fig.transFigure,
        ha="left",
        va="bottom",
        fontsize=8,
        color="#475569",
    )
    return status_artist


if __name__ == "__main__":
    (
        stock_codes_str,
        host,
        ports,
        poll_interval,
        price_interval,
        ui_interval,
        price_mode,
        profit_highlight_threshold,
        telegram_bot_token,
        telegram_chat_id,
    ) = parse_args()
    try:
        set_profit_highlight_threshold(profit_highlight_threshold)
    except ValueError as e:
        logger.error("Invalid --profit_highlight_threshold: %s", e)
        sys.exit(1)
    short_alert_handler = make_telegram_short_close_alert_handler(
        telegram_bot_token,
        telegram_chat_id,
        logger_obj=logger,
    )
    if short_alert_handler:
        logger.info(
            "Short close alerts enabled on Telegram at threshold %.2f%%",
            profit_highlight_threshold,
        )
    elif str(telegram_bot_token).strip() or str(telegram_chat_id).strip():
        logger.warning(
            "Telegram short close alerts disabled: both --telegram_bot_token and "
            "--telegram_chat_id are required"
        )
    stock_codes = [s.strip() for s in stock_codes_str.split(",") if s.strip()]
    if not stock_codes:
        logger.error("No valid stock codes provided. Example: US.AAPL,US.TSLA")
        sys.exit(1)
    started_at = datetime.datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
    startup_settings = build_server_settings(
        started_at=started_at,
        stock_codes=stock_codes,
        futu_host=host,
        futu_ports=ports,
        poll_interval=poll_interval,
        price_interval=price_interval,
        ui_interval=ui_interval,
        price_mode=price_mode,
        profit_highlight_threshold=profit_highlight_threshold,
        telegram_alert_enabled=bool(short_alert_handler),
    )
    startup_footer_text = format_server_settings_text(
        startup_settings, prefix="startup args"
    )
    port_count = len(ports)
    row_count = len(stock_codes)

    fig, axs = plt.subplots(
        row_count,
        port_count,
        figsize=(max(10, 9 * port_count), max(6, 3.8 * row_count)),
        sharex=False,
        squeeze=False,
    )

    backend = OptionDashboardBackend(
        stock_codes=stock_codes,
        host=host,
        ports=ports,
        poll_interval=poll_interval,
        price_interval=price_interval,
        price_mode=price_mode,
        safe_trade_ctx=safe_trade_ctx,
        safe_quote_ctx=safe_quote_ctx,
        query_positions_with_log=_query_positions_with_log,
        get_options_map=get_options_map,
        get_option_quotes_batch=_get_option_quotes_batch,
        merge_option_quotes=_merge_option_quotes,
        get_stock_prices_with_fallback=_get_stock_prices_with_fallback,
        get_stock_share_delta_map=get_stock_share_delta_map,
        get_options_delta_sum=get_options_delta_sum,
        options_signature=_options_signature,
        options_hover_signature=_options_hover_signature,
        panel_key=_panel_key,
        pick_price_option_code=_pick_price_option_code,
        logger=logger,
        init_purpose_prefix="init",
        poll_purpose_prefix="poll_options",
        price_thread_name="poll_price",
        options_thread_name_prefix="poll_options_",
        short_alert_threshold=profit_highlight_threshold if short_alert_handler else None,
        short_alert_handler=short_alert_handler,
    )

    try:
        backend.start()
        backend_state = backend.get_state_snapshot()
        initial_options_by_panel = backend_state["options"]
        initial_plot_signatures = backend_state["options_sig"]
        initial_hover_signatures = backend_state["hover_sig"]
        initial_prices = backend_state["prices"]
        initial_price_done_at = backend_state.get("price_done_at")
        initial_delta_sum_by_panel = backend_state.get("delta_sum_by_panel", {})
        initial_options_done_at_by_port = backend_state.get("options_done_at_by_port", {})
        initial_header = build_dashboard_header_data(
            ui_interval=ui_interval,
            options_version=backend_state["options_version"],
            price_version=backend_state["price_version"],
            price_done_at=initial_price_done_at,
            ports=ports,
            options_done_at_by_port=initial_options_done_at_by_port,
        )

        base_lines = {}
        plot_states = {}
        last_drawn_prices = {}
        for port_index, port in enumerate(ports):
            for row_index, stock_code in enumerate(stock_codes):
                key = _panel_key(port_index, stock_code)
                ax = axs[row_index][port_index]
                options = initial_options_by_panel.get(key, [])
                stock_price = initial_prices.get(stock_code)
                delta_sum = _safe_float(initial_delta_sum_by_panel.get(key), 0.0)
                short_value = get_options_short_value_sum(options)

                if not options:
                    logger.warning(f"No option positions for {stock_code} on port {port}.")

                base_line, base_text, state = plot_chart(
                    ax,
                    options,
                    stock_code,
                    stock_price,
                    chart_title=_panel_title(
                        stock_code,
                        port,
                        delta_sum=delta_sum,
                        short_value=short_value,
                    ),
                    show_y_label=(port_index == 0),
                    y_ticks_on_right=(port_index != 0),
                )
                base_lines[key] = (base_line, base_text)
                plot_states[key] = state
                if base_line is not None and stock_price is not None:
                    last_drawn_prices[key] = stock_price

        header_status_artist = _apply_layout_with_header_footer(
            fig,
            initial_header["title"],
            initial_header["status_text"],
            startup_footer_text,
        )
        _add_marker_legend(fig)
        maximize_figure_window(fig)
        fig.canvas.draw()
        header_state = {
            "status_text": initial_header["status_text"],
            "status_artist": header_status_artist,
        }

        last_drawn_options = dict(initial_plot_signatures)
        last_hover_options = dict(initial_hover_signatures)
        last_drawn_delta_sum = {
            key: _safe_float(delta, 0.0)
            for key, delta in initial_delta_sum_by_panel.items()
        }
        last_drawn_short_value = {
            key: get_options_short_value_sum(options)
            for key, options in initial_options_by_panel.items()
        }
        last_handled_options_version = {"value": -1}
        last_handled_price_version = {"value": -1}

        def _on_close(event):
            backend.stop()

        fig.canvas.mpl_connect('close_event', _on_close)

        def on_timer():
            backend_state = backend.get_state_snapshot()
            latest_prices_snapshot = backend_state["prices"]
            latest_options_snapshot = backend_state["options"]
            latest_options_sig_snapshot = backend_state["options_sig"]
            latest_hover_sig_snapshot = backend_state["hover_sig"]
            latest_delta_sum_snapshot = backend_state.get("delta_sum_by_panel", {})
            latest_options_done_at_by_port = backend_state.get("options_done_at_by_port", {})
            latest_options_version = backend_state["options_version"]
            latest_price_version = backend_state["price_version"]
            latest_price_done_at = backend_state.get("price_done_at")
            try:
                need_redraw = False
                header_data = build_dashboard_header_data(
                    ui_interval=ui_interval,
                    options_version=latest_options_version,
                    price_version=latest_price_version,
                    price_done_at=latest_price_done_at,
                    ports=ports,
                    options_done_at_by_port=latest_options_done_at_by_port,
                )
                latest_status_text = header_data["status_text"]
                if latest_status_text != header_state["status_text"]:
                    header_state["status_artist"].set_text(
                        _wrap_figure_text(latest_status_text)
                    )
                    header_state["status_text"] = latest_status_text
                    need_redraw = True
                if latest_options_version != last_handled_options_version["value"]:
                    for port_index, port in enumerate(ports):
                        for row_index, stock_code in enumerate(stock_codes):
                            key = _panel_key(port_index, stock_code)
                            ax = axs[row_index][port_index]
                            options = latest_options_snapshot.get(key, [])
                            delta_sum = _safe_float(latest_delta_sum_snapshot.get(key), 0.0)
                            prev_delta_sum = _safe_float(last_drawn_delta_sum.get(key), 0.0)
                            short_value = get_options_short_value_sum(options)
                            prev_short_value = _safe_float(
                                last_drawn_short_value.get(key), 0.0
                            )
                            if (
                                round(prev_delta_sum, 3) != round(delta_sum, 3)
                                or round(prev_short_value, 2) != round(short_value, 2)
                            ):
                                ax.set_title(
                                    _panel_title(
                                        stock_code,
                                        port,
                                        delta_sum=delta_sum,
                                        short_value=short_value,
                                    )
                                )
                                last_drawn_delta_sum[key] = delta_sum
                                last_drawn_short_value[key] = short_value
                                need_redraw = True
                            if key not in latest_options_snapshot:
                                continue
                            plot_signature = latest_options_sig_snapshot.get(key)
                            hover_signature = latest_hover_sig_snapshot.get(key)
                            if plot_signature is None:
                                plot_signature = _options_signature(options)
                            if hover_signature is None:
                                hover_signature = _options_hover_signature(options)
                            state = plot_states.get(key)
                            if last_drawn_options.get(key) == plot_signature:
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

                            if not options:
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
                                    latest_prices_snapshot.get(stock_code),
                                    chart_title=_panel_title(
                                        stock_code,
                                        port,
                                        delta_sum=delta_sum,
                                        short_value=short_value,
                                    ),
                                    show_y_label=(port_index == 0),
                                    y_ticks_on_right=(port_index != 0),
                                )
                                plot_states[key] = state
                                base_lines[key] = (base_line, base_text)
                                need_redraw = True
                            else:
                                update_plot(
                                    ax,
                                    options,
                                    state,
                                    stock_price=latest_prices_snapshot.get(stock_code),
                                )
                                need_redraw = True
                            last_drawn_options[key] = plot_signature
                            last_hover_options[key] = hover_signature

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

                if latest_price_version != last_handled_price_version["value"]:
                    for port_index, port in enumerate(ports):
                        for row_index, stock_code in enumerate(stock_codes):
                            key = _panel_key(port_index, stock_code)
                            line, text = base_lines.get(key, (None, None))
                            if line is None or stock_code not in latest_prices_snapshot:
                                continue

                            last_drawn_price = last_drawn_prices.get(key)
                            latest_price = latest_prices_snapshot[stock_code]

                            if (
                                last_drawn_price is not None
                                and round(last_drawn_price, 2) == round(latest_price, 2)
                            ):
                                logger.debug(
                                    f"chart {stock_code}@{port} price unchanged at y={latest_price}, skip"
                                )
                                continue

                            ax = axs[row_index][port_index]
                            moved = move_base_line(ax, line, text, latest_price)
                            if moved:
                                last_drawn_prices[key] = latest_price
                                need_redraw = True
                                logger.info(
                                    f"chart {stock_code}@{port} moved base line to y={latest_price}"
                                )
                            panel_options = latest_options_snapshot.get(key, [])
                            panel_y_values = [
                                option.get("strike_price")
                                for option in panel_options
                            ]
                            if _maybe_expand_panel_y_range_for_price(
                                ax,
                                panel_y_values,
                                latest_price,
                            ):
                                need_redraw = True
                    last_handled_price_version["value"] = latest_price_version

                if need_redraw:
                    fig.canvas.draw_idle()
            except Exception as e:
                logger.error(f"update error: {e}")

        timer = fig.canvas.new_timer(interval=ui_interval * 1000)
        timer.add_callback(on_timer)
        timer.start()

        plt.show()
    except Exception as e:
        logger.error(f"error in futu api: {e}")
        sys.exit(1)
    finally:
        backend.stop()
