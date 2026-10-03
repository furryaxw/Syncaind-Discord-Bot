"""SMAS 客户端：服务号换会话 + WebSocket RPC。

契约（`docs/runtime-communication-contract.md`，2026-10-03 复核）：

* 换会话：``POST /v1/auth/service/exchange``，体 ``{"service_id","secret"}``，
  返回与 GitHub 兑换同形状的会话（含 ``token`` 与 ``expires_at``）。
* 之后所有受保护操作都带 ``Authorization: Bearer <token>``；RPC 走 ``/ws``。
* 信封：``{"action","node","data","headers","request_id"}``；
  响应同形状并带 ``ok`` / ``error``；**服务端推送没有 ``request_id``**，靠这个区分。
* Team 上下文：**用逐请求的 ``x-team-id`` 头**（信封 ``headers`` 里），不要用连接级的 ``select``。
  这里每次 RPC 开一条 WS 连接，而 ``select`` 的作用域是**连接**——它会在连接关闭时丢掉。
  读**他人**权限（``system.users.permissions``）必须带 System Team（``"system"``），
  否则服务端会以 ``team_context_required`` / ``permission_denied`` 拒掉。

会话过期（``invalid_session``）会自动重换一次再重试 —— 服务号的密钥是常驻配置，
重新兑换不需要人工介入，所以这里可以自愈。
"""

from __future__ import annotations

import asyncio
import json
import logging
import uuid
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import aiohttp

SERVICE_EXCHANGE_PATH = "/v1/auth/service/exchange"
WS_PATH = "/ws"
SYSTEM_TEAM_ID = "system"
DEFAULT_TIMEOUT = 30.0
INVALID_SESSION = "invalid_session"


class AccessError(RuntimeError):
    """access server 拒绝了请求。``code`` 是它那张稳定错误码表里的值。"""

    def __init__(self, code: str, message: str = "", *, status: int | None = None) -> None:
        super().__init__(f"{code}: {message}" if message else code)
        self.code = code
        self.message = message
        self.status = status


@dataclass(frozen=True)
class AccessSession:
    token: str
    expires_at: int = 0


def websocket_url(base_url: str) -> str:
    """``http(s)://host:port`` → ``ws(s)://host:port/ws``。"""
    parts = urlsplit(base_url)
    scheme = {"http": "ws", "https": "wss"}.get(parts.scheme, parts.scheme)
    path = WS_PATH if parts.path in {"", "/"} else parts.path.rstrip("/") + WS_PATH
    return urlunsplit((scheme, parts.netloc, path, "", ""))


class AccessClient:
    """``session`` 可注入（测试用假会话，不需要网络）。"""

    def __init__(
        self,
        base_url: str,
        *,
        service_id: str,
        service_secret: str,
        session: Any | None = None,
        user_agent: str = "SyncaindDiscordBot",
        timeout: float = DEFAULT_TIMEOUT,
        logger: logging.Logger | None = None,
    ) -> None:
        self.base_url = base_url.rstrip("/")
        self.service_id = service_id
        self._secret = service_secret
        self._session = session
        self._user_agent = user_agent
        self._timeout = timeout
        self._logger = logger or logging.getLogger("bot.access_roles")
        self._token: str | None = None

    # ------------------------------------------------------------------ 会话

    async def exchange(self) -> AccessSession:
        """用服务号密钥换一个会话。**密钥不出这个函数。**"""
        session = self._session_or_create()
        payload = {"service_id": self.service_id, "secret": self._secret}
        try:
            async with session.post(
                f"{self.base_url}{SERVICE_EXCHANGE_PATH}",
                json=payload,
                headers={"Accept": "application/json", "User-Agent": self._user_agent},
            ) as response:
                status = getattr(response, "status", 200)
                body = await response.json(content_type=None)
        except Exception as exc:  # 连不上/不是 JSON：都不该把 aiohttp 的类型泄漏给上层
            raise AccessError("access_unreachable", f"连不上 access server：{type(exc).__name__}") from exc
        if status != 200 or not isinstance(body, dict) or not body.get("token"):
            code = ""
            message = ""
            if isinstance(body, dict) and isinstance(body.get("error"), dict):
                code = str(body["error"].get("code") or "")
                message = str(body["error"].get("message") or "")
            raise AccessError(code or "service_credential_rejected", message, status=status)

        self._token = str(body["token"])
        self._logger.info("已用服务号 %s 换取 access server 会话", self.service_id)
        return AccessSession(token=self._token, expires_at=int(body.get("expires_at") or 0))

    async def ensure_session(self) -> str:
        if self._token is None:
            await self.exchange()
        assert self._token is not None
        return self._token

    def forget_session(self) -> None:
        self._token = None

    # ------------------------------------------------------------------ RPC

    async def call(
        self,
        action: str,
        node: str,
        data: dict[str, Any] | None = None,
        *,
        team_id: str | None = None,
        headers: dict[str, str] | None = None,
        request_id: str | None = None,
    ) -> dict[str, Any]:
        """发一个信封并等它的响应（跳过服务端推送）。``invalid_session`` 会自动重换一次。

        ``team_id`` 会作为 **``x-team-id`` 头**随这一个请求发出去（服务端允许这个头覆盖
        连接默认值），所以不依赖连接级 ``select``。
        """
        merged = dict(headers or {})
        if team_id:
            merged["x-team-id"] = team_id
        try:
            return await self._call_once(action, node, data, merged or None, request_id)
        except AccessError as exc:
            if exc.code != INVALID_SESSION:
                raise
            self._logger.info("会话已失效，重新兑换后重试：%s %s", action, node)
            await self.exchange()
            return await self._call_once(action, node, data, merged or None, request_id)

    async def _call_once(
        self,
        action: str,
        node: str,
        data: dict[str, Any] | None,
        headers: dict[str, str] | None,
        request_id: str | None,
    ) -> dict[str, Any]:
        token = await self.ensure_session()
        correlation = request_id or uuid.uuid4().hex
        envelope: dict[str, Any] = {"action": action, "node": node, "data": data or {}, "request_id": correlation}
        if headers:
            envelope["headers"] = dict(headers)

        session = self._session_or_create()
        ws_headers = {"Authorization": f"Bearer {token}", "User-Agent": self._user_agent}
        try:
            async with session.ws_connect(websocket_url(self.base_url), headers=ws_headers) as websocket:
                await websocket.send_str(json.dumps(envelope, ensure_ascii=False))
                return await asyncio.wait_for(self._await_response(websocket, correlation), timeout=self._timeout)
        except AccessError:
            raise
        except asyncio.TimeoutError as exc:
            raise AccessError("access_timeout", f"{action} {node} 超时") from exc
        except Exception as exc:
            raise AccessError("access_unreachable", f"RPC 失败：{type(exc).__name__}") from exc

    async def _await_response(self, websocket: Any, correlation: str) -> dict[str, Any]:
        while True:
            message = await websocket.receive()
            if getattr(message, "type", None) == aiohttp.WSMsgType.TEXT:
                payload = json.loads(message.data)
            elif getattr(message, "type", None) == aiohttp.WSMsgType.BINARY:
                payload = json.loads(message.data.decode("utf-8"))
            else:
                raise AccessError("access_closed", "连接在收到响应前关闭")

            if not isinstance(payload, dict):
                continue
            if payload.get("request_id") != correlation:
                # 没有 request_id（或不是我们的）：服务端推送，不在这条路径上处理。
                self._logger.debug("收到服务端推送：%s", payload.get("node") or payload.get("kind"))
                continue
            if payload.get("ok"):
                data = payload.get("data")
                return data if isinstance(data, dict) else {}
            error = payload.get("error") if isinstance(payload.get("error"), dict) else {}
            raise AccessError(str(error.get("code") or "unknown"), str(error.get("message") or ""))

    async def close(self) -> None:
        self._token = None
        if self._session is not None and not getattr(self._session, "closed", True):
            await self._session.close()

    def _session_or_create(self) -> Any:
        if self._session is None or getattr(self._session, "closed", False):
            self._session = aiohttp.ClientSession()
        return self._session
