"""入站 HTTP 端点：给「需要公网回调」的集成用（GitHub web flow 的 OAuth 回调，以后的 webhook）。

**默认不启动**：只有配了 ``WEB_PORT`` 才会监听。默认绑 ``127.0.0.1``——对外暴露交给反向代理
或隧道，机器人自己不开公网监听。

路由是运行时可增删的：所有请求都走一个兜底 handler，按 ``(method, path)`` 查表分发。
这样模块启用/停用时注册与注销都立即生效，不用重建 aiohttp 应用。
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from html import escape

from aiohttp import web

Handler = Callable[[web.Request], Awaitable[web.StreamResponse]]


class BotWebServer:
    def __init__(self, *, host: str = "127.0.0.1", port: int = 0, logger: logging.Logger | None = None) -> None:
        self._host = host
        self._port = int(port)
        self._logger = logger or logging.getLogger("bot.web")
        self._routes: dict[tuple[str, str], Handler] = {}
        self._app: web.Application | None = None
        self._runner: web.AppRunner | None = None
        self._site: web.TCPSite | None = None

    @property
    def is_enabled(self) -> bool:
        return self._port > 0

    @property
    def is_running(self) -> bool:
        return self._site is not None

    @property
    def port(self) -> int:
        return self._port

    @property
    def host(self) -> str:
        return self._host

    @property
    def routes(self) -> tuple[tuple[str, str], ...]:
        return tuple(sorted(self._routes))

    def add_route(self, method: str, path: str, handler: Handler) -> None:
        key = (method.upper(), path)
        if key in self._routes:
            raise ValueError(f"路由已存在：{key[0]} {key[1]}")
        self._routes[key] = handler
        self._logger.debug("已注册入站路由：%s %s", key[0], key[1])

    def remove_route(self, method: str, path: str) -> None:
        self._routes.pop((method.upper(), path), None)

    def build_app(self) -> web.Application:
        app = web.Application()
        app.router.add_route("*", "/{tail:.*}", self._dispatch)
        return app

    async def start(self) -> bool:
        """启动监听。未配置端口时什么都不做并返回 ``False``。"""
        if not self.is_enabled:
            self._logger.debug("未配置 WEB_PORT，入站端点不启动")
            return False
        if self.is_running:
            return True

        self._app = self.build_app()
        self._runner = web.AppRunner(self._app)
        await self._runner.setup()
        try:
            self._site = web.TCPSite(self._runner, self._host, self._port)
            await self._site.start()
        except OSError as exc:
            # 端口被占用之类的：**入站端点起不来不该让机器人起不来**。
            # 需要它的那条路径会自己发现（is_running 为 False），
            # /link github 会退回设备流，其余功能完全不受影响。
            self._logger.error(
                "入站端点没起来（%s:%s，%s）。web flow 会退回设备流；其余功能不受影响。",
                self._host,
                self._port,
                exc,
            )
            await self._runner.cleanup()
            self._runner = None
            self._site = None
            self._app = None
            return False

        self._logger.info("入站端点在 http://%s:%s 上监听（路由 %d 条）", self._host, self._port, len(self._routes))
        return True

    async def stop(self) -> None:
        if self._runner is not None:
            await self._runner.cleanup()
        self._runner = None
        self._site = None
        self._app = None

    async def _dispatch(self, request: web.Request) -> web.StreamResponse:
        handler = self._routes.get((request.method, request.path))
        if handler is None:
            self._logger.debug("入站请求没有对应路由：%s %s", request.method, request.path)
            return web.Response(status=404, text="not found")
        return await handler(request)


def minimal_page(title: str, message: str) -> str:
    """给浏览器看的最小页面。

    标题与正文**必须转义**：它们可能包含来自外部的字符串（比如 GitHub 登录名），
    拼进 HTML 就是自找 XSS。调用方只该传自己文案目录里的文本。
    """
    safe_title = escape(title)
    safe_message = escape(message)
    return (
        '<!doctype html><html><head><meta charset="utf-8">'
        f"<title>{safe_title}</title>"
        "<style>body{font-family:system-ui,sans-serif;margin:4rem auto;max-width:32rem;line-height:1.6}</style>"
        f"</head><body><h1>{safe_title}</h1><p>{safe_message}</p></body></html>"
    )


__all__ = ["BotWebServer", "Handler", "minimal_page"]
