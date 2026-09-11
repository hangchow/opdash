import logging
import time

from futu import RET_OK

logger = logging.getLogger(__name__)

QUERY_RETRY_INTERVAL = 1.5  # 重试间隔秒数
QUERY_MAX_SECONDS = 30.0    # 重试的墙钟上限；超时抛错，由调用方的轮询循环在下一轮重来


def query_hold_positions(trade_ctx, max_seconds=QUERY_MAX_SECONDS):
    # 有界重试：此前是 999 次 × 1.5s（约 25 分钟），且该调用持有 trade_lock，
    # 网关一挂就会把关停流程一起拖住，远超 backend 的退出预算
    deadline = time.monotonic() + max(0.0, max_seconds)
    attempts = 0
    while True:
        ret_code, hold_all = trade_ctx.position_list_query(refresh_cache=True)
        attempts += 1
        if ret_code == RET_OK:
            return hold_all
        logger.error(f"position_list_query, ret_code: {ret_code}, error: {hold_all}")
        if time.monotonic() + QUERY_RETRY_INTERVAL >= deadline:
            raise Exception(
                f"Failed to query positions after {attempts} attempts within {max_seconds:g}s: {hold_all}"
            )
        time.sleep(QUERY_RETRY_INTERVAL)
