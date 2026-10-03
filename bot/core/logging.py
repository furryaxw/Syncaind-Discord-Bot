"""日志层：控制台 + 按天轮转文件，两端都强制 UTF-8。"""

from __future__ import annotations

import logging
import re
import sys
from logging.handlers import TimedRotatingFileHandler
from pathlib import Path
from typing import Any

LOG_FORMAT = "%(asctime)s %(levelname)-8s %(name)s: %(message)s"
LOG_FILE_NAME = "bot.log"
BACKUP_COUNT = 30
ACCESS_LOGGER_NAME = "aiohttp.access"

# 访问日志里的 query string 一律不记：OAuth 回调把授权码与 state 放在 query 里，
# 而路由只按 path 分发 —— 记下来只是让一次性凭据在日志里躺满保留期。
_QUERY_STRING = re.compile(r"\?[^ \"]*")


def ensure_utf8_streams() -> None:
    """把 stdout/stderr 尽量切成 UTF-8。

    Windows 控制台默认按本地编码输出，被重定向到文件时就更容易出乱码；
    而这个机器人有中文提示，读不出来等于没有提示。尽力而为，失败就算了。
    """
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is None:
            continue
        try:
            reconfigure(encoding="utf-8", errors="replace")
        except (ValueError, OSError):
            # 流可能是被重定向的管道或已被替换过的对象，保持原样即可。
            pass


def _strip_query(value: Any) -> Any:
    if isinstance(value, str):
        return _QUERY_STRING.sub("", value)
    if isinstance(value, tuple):
        return tuple(_strip_query(item) for item in value)
    if isinstance(value, dict):
        return {key: _strip_query(item) for key, item in value.items()}
    return value


class StripQueryStringFilter(logging.Filter):
    """把记录参数里 ``/path?query`` 的 query 部分去掉。"""

    def filter(self, record: logging.LogRecord) -> bool:
        record.args = _strip_query(record.args)
        return True


def install_access_log_filter() -> None:
    """给 aiohttp 的访问日志装上过滤器。重复调用是幂等的。"""
    logger = logging.getLogger(ACCESS_LOGGER_NAME)
    if not any(isinstance(installed, StripQueryStringFilter) for installed in logger.filters):
        logger.addFilter(StripQueryStringFilter())


def setup_logging(log_dir: Path, level: str = "INFO") -> logging.Logger:
    """装配根 logger。重复调用是幂等的（先清掉旧 handler）。"""
    log_dir.mkdir(parents=True, exist_ok=True)
    resolved_level = getattr(logging, level.upper(), logging.INFO)

    root = logging.getLogger()
    root.setLevel(resolved_level)
    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(LOG_FORMAT)

    install_access_log_filter()
    ensure_utf8_streams()
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    root.addHandler(console)

    file_handler = TimedRotatingFileHandler(
        filename=str(log_dir / LOG_FILE_NAME),
        when="midnight",
        backupCount=BACKUP_COUNT,
        encoding="utf-8",
        utc=True,
        delay=True,
    )
    file_handler.setFormatter(formatter)
    root.addHandler(file_handler)

    return logging.getLogger("bot")
