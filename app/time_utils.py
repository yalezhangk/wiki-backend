from __future__ import annotations

from datetime import datetime
from zoneinfo import ZoneInfo

BEIJING_TIME_ZONE = ZoneInfo("Asia/Shanghai")


def beijing_now() -> datetime:
    """返回用于 MySQL DATETIME 写入的北京时间（秒精度、无时区标记）。"""
    return datetime.now(BEIJING_TIME_ZONE).replace(tzinfo=None, microsecond=0)
