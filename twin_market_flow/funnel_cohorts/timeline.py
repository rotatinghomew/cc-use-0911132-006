"""UTC 时间轴工具。

所有观察窗按 UTC 计算（见 ``fixtures/analysis/fields.md``），输入时刻均为
带时区的 ISO 8601 字符串。本模块只做显式换算，不依赖本地时区。
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone


def parse_ts(value: str) -> datetime:
    """解析带时区 ISO 8601 字符串，统一为 UTC 感知时刻。"""
    text = str(value).strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    parsed = datetime.fromisoformat(text)
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def iso(value: datetime) -> str:
    """输出与输入口径一致的 UTC ISO 8601 字符串。"""
    return value.astimezone(timezone.utc).isoformat()


def day_offset(later: datetime, earlier: datetime) -> int:
    """两个 UTC 时刻之间的完整天数差（向下取整）。"""
    delta = later - earlier
    return delta.days


def add_days(value: datetime, days: int) -> datetime:
    return value + timedelta(days=days)


def monday_label(value: datetime) -> str:
    """入组周标签：该时刻所在 ISO 周的周一日期（UTC）。"""
    monday = (value - timedelta(days=value.weekday())).date()
    return monday.isoformat()


def round6(value: float) -> float:
    return round(float(value), 6)
