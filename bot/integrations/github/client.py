"""GitHub API 的最小客户端：设备流登录 + 取当前用户。

HTTP 细节全部关在这个文件里，上层只看到「开始设备流 / 轮询一次 / 取用户」三个动作。
传输层是一个 ``Transport`` 协议，测试塞一个假的进去就能完全离线跑——
不需要网络，也不需要 token。

设备流为什么适合这里：它是**唯一不需要公网回调地址**的 GitHub 授权方式。
机器人跑在 VPS 上、没有域名也没关系，用户在浏览器里输入一次性码即可。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import urlencode

import aiohttp

DEVICE_CODE_URL = "https://github.com/login/device/code"
ACCESS_TOKEN_URL = "https://github.com/login/oauth/access_token"
AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
USER_URL = "https://api.github.com/user"

DEVICE_GRANT_TYPE = "urn:ietf:params:oauth:grant-type:device_code"
DEFAULT_SCOPE = "read:user"
API_VERSION = "2022-11-28"

# 轮询返回的三种状态：还在等、GitHub 让我们慢一点、成了。
STATUS_PENDING = "pending"
STATUS_SLOW_DOWN = "slow_down"
STATUS_DONE = "done"

# 这些错误码意味着这次授权彻底失败，重试没有意义。
FATAL_ERRORS = frozenset(
    {
        "access_denied",
        "expired_token",
        "incorrect_device_code",
        "incorrect_client_credentials",
        "unsupported_grant_type",
        "device_flow_disabled",
    }
)

# 等待期间每次轮询的最小间隔，防止 GitHub 返回一个离谱的小值把请求打爆。
MIN_INTERVAL_SECONDS = 5


class DeviceFlowError(RuntimeError):
    """设备流失败。``code`` 是 GitHub 给的错误码，翻成文案由调用方负责。"""

    def __init__(self, code: str, description: str | None = None) -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


class WebFlowError(RuntimeError):
    """web flow（网页授权）失败：state 对不上、code 换 token 被拒等。"""

    def __init__(self, code: str, description: str | None = None) -> None:
        super().__init__(f"{code}: {description}" if description else code)
        self.code = code
        self.description = description


@dataclass(frozen=True)
class DeviceCode:
    device_code: str
    user_code: str
    verification_uri: str
    expires_in: int
    interval: int


@dataclass(frozen=True)
class GitHubUser:
    id: int
    login: str


@dataclass(frozen=True)
class PollResult:
    """一次轮询的结果。``user`` 只在 ``status == "done"`` 时有值。"""

    status: str
    user: GitHubUser | None = None
    interval: int | None = None


class Transport(Protocol):
    """只用到两种请求，单独抽出来是为了能在测试里替换掉。"""

    async def post_form(self, url: str, data: dict[str, str], *, headers: dict[str, str]) -> dict[str, Any]: ...

    async def get_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]: ...


class AiohttpTransport:
    """真实的 HTTP 实现。GitHub 的设备流要求 ``Accept: application/json``。"""

    def __init__(self, session: aiohttp.ClientSession) -> None:
        self._session = session

    async def post_form(self, url: str, data: dict[str, str], *, headers: dict[str, str]) -> dict[str, Any]:
        async with self._session.post(url, data=data, headers=headers) as response:
            response.raise_for_status()
            return await response.json()

    async def get_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        async with self._session.get(url, headers=headers) as response:
            response.raise_for_status()
            return await response.json()


class GitHubClient:
    def __init__(
        self,
        client_id: str,
        transport: Transport,
        *,
        client_secret: str | None = None,
        redirect_uri: str | None = None,
        scope: str = DEFAULT_SCOPE,
        user_agent: str = "SyncaindDiscordBot",
        logger: logging.Logger | None = None,
    ) -> None:
        self._client_id = client_id
        self._client_secret = client_secret
        self._redirect_uri = redirect_uri
        self._transport = transport
        self._scope = scope
        self._user_agent = user_agent
        self._logger = logger or logging.getLogger("bot.github")

    @property
    def client_id(self) -> str:
        return self._client_id

    @property
    def can_use_web_flow(self) -> bool:
        """web flow 要三样东西齐备：client_id、client_secret、回调地址。"""
        return bool(self._client_id and self._client_secret and self._redirect_uri)

    def authorize_url(self, state: str) -> str:
        """拼出让用户点的那条授权链接。``state`` 一次性、会过期。"""
        if not self._redirect_uri:
            raise WebFlowError("missing_redirect_uri")
        query = urlencode(
            {
                "client_id": self._client_id,
                "redirect_uri": self._redirect_uri,
                "scope": self._scope,
                "state": state,
            }
        )
        return f"{AUTHORIZE_URL}?{query}"

    async def exchange_code(self, code: str) -> GitHubUser:
        """用回调带回的 ``code`` 换 token，再取用户。**token 不出这个函数。**"""
        if not self._client_secret:
            raise WebFlowError("missing_client_secret")
        payload = await self._transport.post_form(
            ACCESS_TOKEN_URL,
            {
                "client_id": self._client_id,
                "client_secret": self._client_secret,
                "code": code,
                **({"redirect_uri": self._redirect_uri} if self._redirect_uri else {}),
            },
            headers=self._json_headers(),
        )
        if payload.get("error"):
            raise WebFlowError(str(payload["error"]), payload.get("error_description") or None)
        token = payload.get("access_token")
        if not token:
            raise WebFlowError("missing_access_token")
        return await self.fetch_user(str(token))

    def _json_headers(self) -> dict[str, str]:
        return {"Accept": "application/json", "User-Agent": self._user_agent}

    def _authorized_headers(self, token: str) -> dict[str, str]:
        return {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": self._user_agent,
        }

    async def start_device_flow(self) -> DeviceCode:
        payload = await self._transport.post_form(
            DEVICE_CODE_URL,
            {"client_id": self._client_id, "scope": self._scope},
            headers=self._json_headers(),
        )
        if "device_code" not in payload:
            raise DeviceFlowError(
                str(payload.get("error", "unknown")),
                str(payload.get("error_description", "")) or None,
            )
        return DeviceCode(
            device_code=str(payload["device_code"]),
            user_code=str(payload["user_code"]),
            verification_uri=str(payload.get("verification_uri") or payload.get("verification_url") or ""),
            expires_in=int(payload.get("expires_in", 900)),
            interval=max(MIN_INTERVAL_SECONDS, int(payload.get("interval", MIN_INTERVAL_SECONDS))),
        )

    async def poll_device_flow(self, device_code: str) -> PollResult:
        """轮询一次。

        * 还在等 → ``STATUS_PENDING``
        * GitHub 要求放慢 → ``STATUS_SLOW_DOWN``（带新的间隔建议）
        * 成了 → ``STATUS_DONE`` 且带上用户
        * 彻底失败 → 抛 :class:`DeviceFlowError`
        """
        payload = await self._transport.post_form(
            ACCESS_TOKEN_URL,
            {
                "client_id": self._client_id,
                "device_code": device_code,
                "grant_type": DEVICE_GRANT_TYPE,
            },
            headers=self._json_headers(),
        )

        error = payload.get("error")
        if error == "authorization_pending":
            return PollResult(status=STATUS_PENDING)
        if error == "slow_down":
            suggested = int(payload.get("interval", MIN_INTERVAL_SECONDS)) + MIN_INTERVAL_SECONDS
            return PollResult(status=STATUS_SLOW_DOWN, interval=max(MIN_INTERVAL_SECONDS, suggested))
        if error:
            raise DeviceFlowError(str(error), payload.get("error_description") or None)

        token = payload.get("access_token")
        if not token:
            raise DeviceFlowError("missing_access_token")

        user = await self.fetch_user(str(token))
        return PollResult(status=STATUS_DONE, user=user)

    async def fetch_user(self, token: str) -> GitHubUser:
        payload = await self._transport.get_json(USER_URL, headers=self._authorized_headers(token))
        return GitHubUser(id=int(payload["id"]), login=str(payload["login"]))
