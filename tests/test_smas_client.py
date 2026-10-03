"""access server 客户端：换会话、信封 RPC、跳过推送、会话自愈、以及「读失败 ≠ 没权限」。

契约来自 `docs/runtime-communication-contract.md`（已复核）：
`POST /v1/auth/service/exchange` 换会话、`/ws` 发信封、推送没有 `request_id`、
读他人权限要带 System Team 上下文。
"""

from __future__ import annotations

import json

import pytest

from bot.integrations.smas import (
    SYSTEM_TEAM_ID,
    AccessClient,
    AccessError,
    AccessServerSource,
    AccessSourceError,
    UnconfiguredAccessSource,
    websocket_url,
)
from tests.smas_fakes import (
    FakeHttpSession,
    FakeWebSocket,
    error_reply,
    make_responder,
    ok_reply,
    push,
)


def make_client(session: FakeHttpSession, **kwargs) -> AccessClient:
    return AccessClient(
        "http://access.test:8787",
        service_id="discord-bot",
        service_secret="s" * 32,
        session=session,
        timeout=kwargs.pop("timeout", 5.0),
        **kwargs,
    )


def test_websocket_url_mapping() -> None:
    assert websocket_url("http://access.test:8787") == "ws://access.test:8787/ws"
    assert websocket_url("https://access.example.com") == "wss://access.example.com/ws"
    assert websocket_url("https://access.example.com/api") == "wss://access.example.com/api/ws"


# ---------------------------------------------------------------- 换会话


async def test_exchange_posts_the_service_credential() -> None:
    session = FakeHttpSession(exchange={"token": "tok-9", "expires_at": 123})
    client = make_client(session)

    access = await client.exchange()

    assert access.token == "tok-9"
    assert access.expires_at == 123
    url, body, headers = session.posts[0]
    assert url == "http://access.test:8787/v1/auth/service/exchange"
    assert body == {"service_id": "discord-bot", "secret": "s" * 32}
    assert headers["Accept"] == "application/json"


async def test_exchange_maps_a_rejection_to_its_error_code() -> None:
    """未知服务号 / 错误密钥 / 已暂停，服务器返回同一个 401，客户端照码分支。"""
    session = FakeHttpSession(
        exchange={"error": {"code": "service_credential_rejected", "message": "nope"}},
        exchange_status=401,
    )

    with pytest.raises(AccessError) as excinfo:
        await make_client(session).exchange()

    assert excinfo.value.code == "service_credential_rejected"
    assert excinfo.value.status == 401


async def test_the_service_secret_never_leaks_into_the_error() -> None:
    session = FakeHttpSession(exchange={"error": {"code": "service_credential_rejected"}}, exchange_status=401)

    with pytest.raises(AccessError) as excinfo:
        await make_client(session).exchange()

    assert "s" * 32 not in str(excinfo.value)


# ---------------------------------------------------------------- 信封 RPC


async def test_call_sends_an_envelope_and_returns_data() -> None:
    websocket = FakeWebSocket(make_responder({"value": 7}))
    session = FakeHttpSession(websocket=websocket)
    client = make_client(session)

    data = await client.call("read", "system.authentication.me")

    assert data == {"value": 7}
    envelope = websocket.sent[0]
    assert envelope["action"] == "read"
    assert envelope["node"] == "system.authentication.me"
    assert envelope["data"] == {}
    assert envelope["request_id"], "每个请求都要带 request_id，才能和推送区分"
    _url, headers = session.connections[0]
    assert headers["Authorization"] == "Bearer tok-1"


async def test_call_reuses_an_existing_session() -> None:
    websocket = FakeWebSocket(make_responder())
    session = FakeHttpSession(websocket=websocket)
    client = make_client(session)

    await client.call("read", "system.authentication.me")
    await client.call("read", "system.authentication.me")

    assert len(session.posts) == 1, "不该每次调用都换一次会话"


async def test_team_context_goes_in_the_request_headers() -> None:
    """用逐请求的 x-team-id，而不是连接级 select（这里每次 RPC 都开新连接）。"""
    websocket = FakeWebSocket(make_responder())
    session = FakeHttpSession(websocket=websocket)

    await make_client(session).call("read", "system.users.permissions", team_id=SYSTEM_TEAM_ID)

    assert websocket.sent[0]["headers"] == {"x-team-id": "system"}


async def test_server_pushes_are_skipped() -> None:
    """推送没有 request_id —— 不能把它当成自己的响应，否则整个 RPC 就错位了。"""
    websocket = FakeWebSocket(make_responder({"real": True}, pushes=2))
    session = FakeHttpSession(websocket=websocket)

    data = await make_client(session).call("read", "system.authentication.me")

    assert data == {"real": True}


async def test_an_error_response_becomes_an_access_error() -> None:
    websocket = FakeWebSocket(make_responder(code="permission_denied"))
    session = FakeHttpSession(websocket=websocket)

    with pytest.raises(AccessError) as excinfo:
        await make_client(session).call("read", "system.users.permissions")

    assert excinfo.value.code == "permission_denied"


async def test_invalid_session_is_refreshed_and_retried() -> None:
    """服务号的密钥是常驻配置：重新兑换不需要人工介入，所以这里自愈。"""
    state = {"first": True}

    def responder(envelope: dict) -> list:
        # 第一次回 invalid_session，第二次回成功
        if state["first"]:
            state["first"] = False
            return [error_reply(envelope, "invalid_session")]
        return [ok_reply(envelope, {"ok": True})]

    websocket = FakeWebSocket(responder)
    session = FakeHttpSession(websocket=websocket)
    client = make_client(session)

    data = await client.call("read", "system.authentication.me")

    assert data == {"ok": True}
    assert len(session.posts) == 2, "应该重新兑换一次会话"
    assert len(session.connections) == 2


async def test_other_errors_are_not_retried() -> None:
    websocket = FakeWebSocket(make_responder(code="permission_denied"))
    session = FakeHttpSession(websocket=websocket)

    with pytest.raises(AccessError):
        await make_client(session).call("read", "system.users.permissions")

    assert len(session.posts) == 1


async def test_a_timeout_is_reported_as_such() -> None:
    websocket = FakeWebSocket(hang=True)
    session = FakeHttpSession(websocket=websocket)

    with pytest.raises(AccessError) as excinfo:
        await make_client(session, timeout=0.01).call("read", "system.authentication.me")

    assert excinfo.value.code == "access_timeout"


async def test_correlation_ids_are_unique_per_call() -> None:
    websocket = FakeWebSocket(make_responder())
    session = FakeHttpSession(websocket=websocket)
    client = make_client(session)

    await client.call("read", "a")
    await client.call("read", "b")

    assert websocket.sent[0]["request_id"] != websocket.sent[1]["request_id"]


# ---------------------------------------------------------------- 有效节点（source）


def permission_row(value: str, *, source_id: str = "template.member") -> dict:
    return {"value": value, "effect": "allow", "granted_by": source_id, "source_type": "permission_template"}


def assignment_row(node: str, *, source_id: str = "team-owner:acme") -> dict:
    return {"node": node, "effect": "allow", "source_id": source_id, "source_type": "permission_template"}


async def test_effective_nodes_merges_both_halves() -> None:
    """`permissions` 是系统作用域、`assignments` 跨全部 Team —— 合起来才是有效节点。"""
    websocket = FakeWebSocket(
        make_responder(
            {
                "user_id": "12345",
                "permissions": [permission_row("system.users.read")],
                "assignments": [assignment_row("team.acme.packages.read")],
            }
        )
    )
    session = FakeHttpSession(websocket=websocket)
    source = AccessServerSource(make_client(session))

    nodes = await source.effective_nodes("12345")

    assert nodes.nodes == {"system.users.read", "team.acme.packages.read"}
    assert websocket.sent[0]["data"] == {"user_id": "12345"}
    assert websocket.sent[0]["headers"] == {"x-team-id": "system"}


async def test_effective_nodes_tolerates_extra_junk() -> None:
    websocket = FakeWebSocket(
        make_responder(
            {
                "permissions": [permission_row("system.users.read"), {"nothing": True}, "junk"],
                "assignments": [assignment_row("team.a.x"), {}],
            }
        )
    )
    source = AccessServerSource(make_client(FakeHttpSession(websocket=websocket)))

    nodes = await source.effective_nodes("1")

    assert nodes.nodes == {"system.users.read", "team.a.x"}


async def test_a_read_failure_raises_and_never_looks_like_no_permissions() -> None:
    """**最关键的一条**：读失败必须抛出去。

    如果这里退化成「返回空集合」，一次网络抖动就会让同步把所有角色的撤掉。
    """
    websocket = FakeWebSocket(make_responder(code="access_unreachable"))
    source = AccessServerSource(make_client(FakeHttpSession(websocket=websocket)))

    with pytest.raises(AccessSourceError):
        await source.effective_nodes("12345")


async def test_the_unconfigured_source_fails_loudly() -> None:
    with pytest.raises(AccessSourceError):
        await UnconfiguredAccessSource().effective_nodes("1")


def test_ok_and_error_reply_builders_track_request_ids() -> None:
    """替身自己的行为也要对：响应必须带回同一个 request_id。"""
    envelope = {"request_id": "abc", "action": "read", "node": "x"}

    assert ok_reply(envelope)["request_id"] == "abc"
    assert error_reply(envelope, "boom")["error"]["code"] == "boom"
    assert "request_id" not in push()
    assert json.dumps(ok_reply(envelope))  # 可序列化
