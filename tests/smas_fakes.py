"""access server 侧的测试替身：假的 HTTP 会话与假的 WebSocket。

客户端的传输细节全部走 ``session.post`` / ``session.ws_connect``，所以把这两处替掉
就能完全离线跑，同时也验证了「HTTP/WS 细节只在 client.py 里」这件事。
"""

from __future__ import annotations

import json
from collections.abc import Callable
from types import SimpleNamespace
from typing import Any

from aiohttp import WSMsgType


class FakeHttpResponse:
    def __init__(self, body: Any, *, status: int = 200) -> None:
        self.status = status
        self._body = body

    async def json(self, content_type: Any = None) -> Any:
        return self._body

    async def __aenter__(self) -> FakeHttpResponse:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeWebSocket:
    """按脚本回复：``responder`` 看到**发出的信封**，返回要收的消息列表（可含推送）。"""

    def __init__(
        self,
        responder: Callable[[dict[str, Any]], list[Any]] | None = None,
        *,
        hang: bool = False,
    ) -> None:
        self.sent: list[dict[str, Any]] = []
        self._responder = responder
        self._hang = hang
        self._queue: list[Any] = []

    async def send_str(self, data: str) -> None:
        envelope = json.loads(data)
        self.sent.append(envelope)
        if self._responder is not None:
            for item in self._responder(envelope):
                self._queue.append(item)

    async def receive(self) -> Any:
        if self._hang or not self._queue:
            import asyncio

            await asyncio.sleep(3600)  # 永远等不到：用来测超时
        payload = self._queue.pop(0)
        if isinstance(payload, str):
            return SimpleNamespace(type=WSMsgType.TEXT, data=payload)
        return SimpleNamespace(type=WSMsgType.TEXT, data=json.dumps(payload, ensure_ascii=False))

    async def __aenter__(self) -> FakeWebSocket:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeHttpSession:
    def __init__(
        self,
        *,
        exchange: Any = None,
        exchange_status: int = 200,
        websocket: FakeWebSocket | None = None,
    ) -> None:
        self._exchange = exchange if exchange is not None else {"token": "tok-1", "expires_at": 42}
        self._exchange_status = exchange_status
        self._websocket = websocket or FakeWebSocket()
        self.posts: list[tuple[str, Any, dict[str, str]]] = []
        self.connections: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    def post(self, url: str, *, json: Any = None, headers: dict[str, str] | None = None) -> FakeHttpResponse:
        self.posts.append((url, json, dict(headers or {})))
        return FakeHttpResponse(self._exchange, status=self._exchange_status)

    def ws_connect(self, url: str, *, headers: dict[str, str] | None = None) -> FakeWebSocket:
        self.connections.append((url, dict(headers or {})))
        return self._websocket

    async def close(self) -> None:
        self.closed = True


def ok_reply(envelope: dict[str, Any], data: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "ok": True,
        "action": envelope.get("action"),
        "node": envelope.get("node"),
        "data": data or {},
        "request_id": envelope.get("request_id"),
    }


def error_reply(envelope: dict[str, Any], code: str, message: str = "") -> dict[str, Any]:
    return {
        "ok": False,
        "action": envelope.get("action"),
        "node": envelope.get("node"),
        "request_id": envelope.get("request_id"),
        "error": {"code": code, "message": message},
    }


def push(node: str = "team.acme", kind: str = "resource.changed") -> dict[str, Any]:
    """服务端主动推送：**没有 request_id**。"""
    return {"kind": kind, "action": "manage", "node": node, "data": {}}


def make_responder(data: dict[str, Any] | None = None, *, pushes: int = 0, code: str = ""):
    def responder(envelope: dict[str, Any]) -> list[Any]:
        messages: list[Any] = [push() for _ in range(pushes)]
        messages.append(error_reply(envelope, code) if code else ok_reply(envelope, data))
        return messages

    return responder


class FakeAccessClient:
    """假的 access 客户端：按 ``(action, node)`` 脚本化返回，并记录每次调用。

    比假 HTTP 会话高一层：发码那条链路（take / release / 查批次归属）用这个测，
    传输细节由 ``test_smas_client.py`` 负责。
    """

    def __init__(self, responses: dict[tuple[str, str], Any] | None = None) -> None:
        self._responses = dict(responses or {})
        self.calls: list[tuple[str, str, dict[str, Any], str | None]] = []

    def set(self, action: str, node: str, payload: Any) -> None:
        self._responses[(action, node)] = payload

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
        del headers, request_id
        self.calls.append((action, node, dict(data or {}), team_id))
        payload = self._responses.get((action, node))
        if payload is None:
            raise AssertionError(f"没有为 {action} {node} 准备响应（已调用 {len(self.calls)} 次）")
        if isinstance(payload, Exception):
            raise payload
        return payload

    def actions(self) -> list[str]:
        return [action for action, _node, _data, _team in self.calls]


def taken_key(
    key_id: str = "key-1",
    plaintext: str = "SMAS6N54M5YKKEFRPBSK09GZ",
    *,
    batch_id: str = "b1",
) -> dict[str, Any]:
    return {"key_id": key_id, "plaintext": plaintext, "batch_id": batch_id, "key_prefix": plaintext[:10]}


def taken_payload(*keys: dict[str, Any]) -> dict[str, Any]:
    return {"keys": list(keys), "count": len(keys)}


def key_inventory_row(*, team_id: str = "acme", batch_id: str = "b1") -> dict[str, Any]:
    return {"key_id": "key-scan", "batch_id": batch_id, "team_id": team_id, "status": "unused"}


class FakeAccessSource:
    """假的「有效节点」来源。

    ``nodes`` 是 ``{github_user_id: {节点...}}``；``errors`` 指定某个人读失败 ——
    那条路径正是「读失败 ≠ 没有权限」要测的东西。
    """

    def __init__(
        self,
        nodes: dict[str, set[str]] | None = None,
        *,
        errors: dict[str, Exception] | None = None,
        error: Exception | None = None,
    ) -> None:
        self._nodes = dict(nodes or {})
        self._errors = dict(errors or {})
        self._error = error
        self.calls: list[str] = []

    async def effective_nodes(self, github_user_id: str) -> Any:
        from bot.integrations.smas import EffectiveNodes

        self.calls.append(github_user_id)
        if github_user_id in self._errors:
            raise self._errors[github_user_id]
        if self._error is not None:
            raise self._error
        return EffectiveNodes(
            github_user_id=github_user_id,
            nodes=frozenset(self._nodes.get(github_user_id, set())),
        )
