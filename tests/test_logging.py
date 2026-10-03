"""日志层：访问日志不能把 query string 记下来。

OAuth 回调把授权码与 state 放在 query 里，而路由只按 path 分发 —— 一次性凭据
躺在日志里没有任何诊断价值，只多出一个泄露面。
"""

from __future__ import annotations

import logging

from bot.core.logging import StripQueryStringFilter, install_access_log_filter


def _record(*args: object) -> logging.LogRecord:
    return logging.LogRecord(
        name="aiohttp.access",
        level=logging.INFO,
        pathname=__file__,
        lineno=1,
        msg=" ".join(["%s"] * len(args)),
        args=args,
        exc_info=None,
    )


def test_query_string_is_stripped_from_the_request_line() -> None:
    record = _record("192.168.5.1", "GET /github/callback?code=abc&state=xyz HTTP/1.1", "200")

    assert StripQueryStringFilter().filter(record) is True
    assert record.getMessage() == "192.168.5.1 GET /github/callback HTTP/1.1 200"


def test_every_string_argument_is_stripped() -> None:
    record = _record("GET /a?x=1 HTTP/1.1", "GET /b?y=2 HTTP/1.1")

    StripQueryStringFilter().filter(record)
    assert record.getMessage() == "GET /a HTTP/1.1 GET /b HTTP/1.1"


def test_arguments_that_are_not_strings_are_left_alone() -> None:
    record = _record(42, "GET /plain HTTP/1.1")

    StripQueryStringFilter().filter(record)
    assert record.getMessage() == "42 GET /plain HTTP/1.1"


def test_installing_twice_does_not_stack_filters() -> None:
    logger = logging.getLogger("aiohttp.access")

    install_access_log_filter()
    install_access_log_filter()

    installed = [item for item in logger.filters if isinstance(item, StripQueryStringFilter)]
    assert len(installed) == 1
