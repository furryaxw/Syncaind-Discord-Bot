"""GitHub API 客户端：设备流与取用户，全部对着一个假 transport 跑，不需要网络。

同时把「不存 token」这条也钉住：``GitHubUser`` / ``DeviceCode`` 里唯一的敏感值是
内存中的 access token，它只用来调一次 ``/user``，不返回给上层、也不落库。
"""

from __future__ import annotations

import pytest

from bot.integrations.github import client as client_module
from bot.integrations.github.client import (
    ACCESS_TOKEN_URL,
    DEVICE_CODE_URL,
    USER_URL,
    DeviceFlowError,
    GitHubClient,
    GitHubUser,
    PollResult,
)
from tests.github_fakes import FakeTransport

CLIENT_ID = "cid-123"


def make_client(transport: FakeTransport) -> GitHubClient:
    return GitHubClient(CLIENT_ID, transport, user_agent="TestAgent")


# ---------------------------------------------------------------- 设备流开始


async def test_start_device_flow_parses_the_response() -> None:
    transport = FakeTransport()

    flow = await make_client(transport).start_device_flow()

    assert flow.device_code == "dev-1"
    assert flow.user_code == "ABCD-1234"
    assert flow.verification_uri == "https://github.com/login/device"
    assert flow.expires_in == 900
    assert flow.interval == 5
    _method, url, data = transport.calls[0]
    assert url == DEVICE_CODE_URL
    assert data["client_id"] == CLIENT_ID


async def test_start_device_flow_asks_for_a_json_response() -> None:
    """GitHub 默认返回 form-encoded；不带这个头会解析不出 JSON。"""
    transport = FakeTransport()

    await make_client(transport).start_device_flow()

    method, url, data = transport.calls[0]
    assert (method, url) == ("post", DEVICE_CODE_URL)
    assert data["scope"]


async def test_start_device_flow_raises_on_error_payload() -> None:
    transport = FakeTransport(device={"error": "device_flow_disabled", "error_description": "nope"})

    with pytest.raises(DeviceFlowError) as excinfo:
        await make_client(transport).start_device_flow()

    assert excinfo.value.code == "device_flow_disabled"


async def test_interval_is_never_below_the_floor() -> None:
    transport = FakeTransport(device={"device_code": "d", "user_code": "u", "verification_uri": "v", "interval": 0})

    flow = await make_client(transport).start_device_flow()

    assert flow.interval == client_module.MIN_INTERVAL_SECONDS


# ---------------------------------------------------------------- 轮询


async def test_poll_reports_pending() -> None:
    transport = FakeTransport(polls=[{"error": "authorization_pending"}])

    result = await make_client(transport).poll_device_flow("dev-1")

    assert result == PollResult(status=client_module.STATUS_PENDING)


async def test_poll_reports_slow_down_with_a_bigger_interval() -> None:
    transport = FakeTransport(polls=[{"error": "slow_down"}])

    result = await make_client(transport).poll_device_flow("dev-1")

    assert result.status == client_module.STATUS_SLOW_DOWN
    assert result.interval is not None and result.interval >= client_module.MIN_INTERVAL_SECONDS


@pytest.mark.parametrize("code", ["access_denied", "expired_token", "incorrect_device_code"])
async def test_poll_raises_on_fatal_errors(code: str) -> None:
    transport = FakeTransport(polls=[{"error": code}])

    with pytest.raises(DeviceFlowError) as excinfo:
        await make_client(transport).poll_device_flow("dev-1")

    assert excinfo.value.code == code


async def test_poll_fetches_the_user_on_success() -> None:
    transport = FakeTransport(polls=[{"access_token": "tok-1"}])

    result = await make_client(transport).poll_device_flow("dev-1")

    assert result.status == client_module.STATUS_DONE
    assert result.user == GitHubUser(id=42, login="furryaxw")
    method, url, headers = transport.calls[-1]
    assert (method, url) == ("get", USER_URL)
    assert headers["Authorization"] == "Bearer tok-1"


async def test_poll_without_a_token_is_an_error() -> None:
    transport = FakeTransport(polls=[{}])

    with pytest.raises(DeviceFlowError) as excinfo:
        await make_client(transport).poll_device_flow("dev-1")

    assert excinfo.value.code == "missing_access_token"


async def test_poll_sends_the_device_grant_type() -> None:
    transport = FakeTransport(polls=[{"access_token": "tok"}])

    await make_client(transport).poll_device_flow("dev-1")

    _method, url, data = transport.calls[0]
    assert url == ACCESS_TOKEN_URL
    assert data["device_code"] == "dev-1"
    assert data["grant_type"].startswith("urn:ietf:params:oauth:grant-type")
    # 设备流刷新不需要 client_secret
    assert "client_secret" not in data
