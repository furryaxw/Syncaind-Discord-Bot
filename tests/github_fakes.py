"""GitHub 侧的测试替身：一个按 URL 分派的假 transport。

客户端只用到两种请求（表单 POST / JSON GET），所以把传输层替换掉就能完全离线跑
——同时也**验证了「HTTP 细节只在 client.py 里」这件事**：如果哪天有人在别处直接
调 aiohttp，这里的测试就会失效。
"""

from __future__ import annotations

from typing import Any

from bot.integrations.github.client import ACCESS_TOKEN_URL, DEVICE_CODE_URL, USER_URL
from bot.integrations.github.releases import MAX_RELEASES, Release

DEFAULT_USER = {"id": 42, "login": "furryaxw"}


def make_release(
    release_id: int = 1,
    *,
    repo: str = "furryaxw/SprocketModManager",
    tag: str = "v1.0.0",
    name: str = "",
    body: str = "",
    url: str = "",
    published_at: str = "2026-01-01T00:00:00Z",
    prerelease: bool = False,
    draft: bool = False,
) -> Release:
    return Release(
        repo=repo,
        release_id=release_id,
        tag=tag,
        name=name,
        body=body,
        html_url=url or f"https://github.com/{repo}/releases/tag/{tag}",
        published_at=published_at,
        prerelease=prerelease,
        draft=draft,
    )


class FakeReleaseSource:
    """按仓库返回预设的 release（从新到旧），也可以注入异常来测失败路径。"""

    def __init__(self, releases: list[Release] | None = None, *, error: Exception | None = None) -> None:
        self._releases = list(releases or [])
        self._error = error
        self.calls: list[str] = []
        self.tag_calls: list[tuple[str, str]] = []

    async def list_releases(self, repo: str, *, limit: int = MAX_RELEASES) -> list[Release]:
        self.calls.append(repo)
        if self._error is not None:
            raise self._error
        return [release for release in self._releases if release.repo == repo][:limit]

    async def get_release(self, repo: str, tag: str) -> Release | None:
        self.tag_calls.append((repo, tag))
        if self._error is not None:
            raise self._error
        return next(
            (release for release in self._releases if release.repo == repo and release.tag == tag),
            None,
        )


# ---------------------------------------------------------------- 监听服务


def make_event(
    seq: int = 1,
    *,
    repo: str = "furryaxw/SprocketModManager",
    kind: str = "Release",
    url: str = "",
    title: str = "v1.0.0",
    payload: dict[str, Any] | None = None,
    received_at: str = "2026-10-01T00:00:00Z",
) -> dict[str, Any]:
    """一条监听服务事件（就是它文档里那种形状）。"""
    event: dict[str, Any] = {
        "seq": seq,
        "id": f"delivery-{seq}",
        "received_at": received_at,
        "type": "release",
        "action": "published",
        "kind": kind,
        "owner": repo.split("/")[0],
        "repo": repo.split("/")[-1],
        "full_name": repo,
        "actor": "furryaxw",
        "number": None,
        "title": title,
        "url": url or f"https://github.com/{repo}/releases/tag/{title}",
        "detail": {},
        "text": f"[{kind}] {repo} {title}",
    }
    if payload is not None:
        event["payload"] = payload
    return event


def release_payload(
    release_id: int = 101,
    *,
    tag: str = "v1.0.0",
    name: str = "",
    body: str = "## 变更\n- 修了 A",
    prerelease: bool = False,
    draft: bool = False,
) -> dict[str, Any]:
    """GitHub 的 release payload（监听服务开了 GHW_INCLUDE_PAYLOAD 时才会带上）。"""
    return {
        "release": {
            "id": release_id,
            "tag_name": tag,
            "name": name,
            "body": body,
            "html_url": f"https://github.com/furryaxw/SprocketModManager/releases/tag/{tag}",
            "published_at": "2026-10-01T12:00:00Z",
            "prerelease": prerelease,
            "draft": draft,
        }
    }


class FakeWatcherClient:
    """监听服务的轮询面（我们只用它做区间检查）。"""

    def __init__(self, page: Any = None, *, error: Exception | None = None) -> None:
        self._page = page
        self._error = error
        self.calls: list[tuple[int, dict[str, Any]]] = []

    async def fetch(self, since: int, **kwargs: Any) -> Any:
        self.calls.append((since, kwargs))
        if self._error is not None:
            raise self._error
        if self._page is not None:
            return self._page
        from bot.integrations.github.watcher import WatcherPage

        return WatcherPage(events=(), cursor=since, latest_seq=since, more=False, since_evicted=False)


class FakeWatcherStream:
    """监听服务的 SSE 面：按脚本吐出事件，吐完就结束（模拟服务端断开）。"""

    def __init__(self, events: list[dict[str, Any]] | None = None, *, error: Exception | None = None) -> None:
        self._events = list(events or [])
        self._error = error
        self.retry_ms = 3000
        self.since_seen: list[int] = []

    async def events(self, since: int, **kwargs: Any) -> Any:
        self.since_seen.append(since)
        if self._error is not None:
            raise self._error
        from bot.integrations.github.watcher import _to_event

        for raw in self._events:
            yield _to_event(raw)


class FakeLineStream:
    """异步逐行吐文本，冒充 aiohttp 的 ``response.content``。"""

    def __init__(self, lines: list[str]) -> None:
        self._lines = list(lines)

    def __aiter__(self) -> FakeLineStream:
        return self

    async def __anext__(self) -> str:
        if not self._lines:
            raise StopAsyncIteration
        return self._lines.pop(0)


class FakeSseResponse:
    def __init__(self, lines: list[str], *, status: int = 200) -> None:
        self.status = status
        self.content = FakeLineStream(lines)

    async def __aenter__(self) -> FakeSseResponse:
        return self

    async def __aexit__(self, *exc_info: Any) -> bool:
        return False


class FakeSseSession:
    """冒充 ``aiohttp.ClientSession``：只实现 ``get(url, headers=...)``。"""

    def __init__(self, response: FakeSseResponse) -> None:
        self._response = response
        self.calls: list[tuple[str, dict[str, str]]] = []
        self.closed = False

    def get(self, url: str, *, headers: dict[str, str] | None = None) -> FakeSseResponse:
        self.calls.append((url, dict(headers or {})))
        return self._response


class FakeJsonTransport:
    """按 URL 片段返回 JSON，用来测 GitHub REST 那一层。

    ``pages`` 的键是 URL 里出现的片段，值就是那一页的响应。**片段必须无歧义** ——
    例如用 ``"&page=2"`` 而不是 ``"page=2"``（后者会被 ``per_page=20`` 之类命中）。
    """

    def __init__(self, pages: dict[str, Any] | None = None, *, error: Exception | None = None) -> None:
        self._pages = dict(pages or {})
        self._error = error
        self.calls: list[tuple[str, dict[str, str]]] = []

    async def get_json(self, url: str, *, headers: dict[str, str]) -> Any:
        self.calls.append((url, dict(headers)))
        if self._error is not None:
            raise self._error
        for fragment, payload in self._pages.items():
            if fragment in url:
                return payload
        raise AssertionError(f"没有为这个 URL 准备响应：{url}")

    async def post_form(self, url: str, data: dict[str, str], *, headers: dict[str, str]) -> Any:
        raise AssertionError(f"这一层不该发 POST：{url}")


class FakeTransport:
    def __init__(
        self,
        *,
        device: dict[str, Any] | None = None,
        polls: list[dict[str, Any]] | None = None,
        user: dict[str, Any] | None = None,
    ) -> None:
        self._device = (
            device
            if device is not None
            else {
                "device_code": "dev-1",
                "user_code": "ABCD-1234",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            }
        )
        self._polls = list(polls or [])
        self._user = user if user is not None else dict(DEFAULT_USER)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    async def post_form(self, url: str, data: dict[str, str], *, headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append(("post", url, dict(data)))
        if url == DEVICE_CODE_URL:
            return self._device
        if url == ACCESS_TOKEN_URL:
            assert self._polls, "轮询次数超出预期"
            return self._polls.pop(0)
        raise AssertionError(f"没有预期的 POST：{url}")

    async def get_json(self, url: str, *, headers: dict[str, str]) -> dict[str, Any]:
        self.calls.append(("get", url, dict(headers)))
        if url == USER_URL:
            return self._user
        raise AssertionError(f"没有预期的 GET：{url}")


def make_device_transport(
    *,
    polls: list[dict[str, Any]],
    user: dict[str, Any] | None = None,
    interval: int = 5,
) -> FakeTransport:
    """构造一个「设备流会怎样返回」的假 transport。``interval=0`` 让轮询循环快起来。"""
    device = {
        "device_code": "dev-1",
        "user_code": "ABCD-1234",
        "verification_uri": "https://github.com/login/device",
        "expires_in": 900,
        "interval": interval,
    }
    return FakeTransport(device=device, polls=polls, user=user)
