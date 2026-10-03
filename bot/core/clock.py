"""时间工具：统一用 UTC 存储。"""

from __future__ import annotations

from datetime import datetime, timezone


def utcnow() -> datetime:
    """当前 UTC 时间（带时区）。"""
    return datetime.now(timezone.utc)


def utcnow_iso() -> str:
    """当前 UTC 时间的 ISO-8601 表示，精确到秒。"""
    return utcnow().isoformat(timespec="seconds")


def parse_iso(value: str | None) -> datetime | None:
    """解析外部给的 ISO-8601 时间（GitHub 的时间戳），失败就返回 ``None``。

    外部时间戳的格式不受我们控制：**宁可少一个时间戳，不能让一条 release 因为时间戳
    解析不了就推不出去**。所以这里吞掉异常而不是抛。
    """
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=timezone.utc)
