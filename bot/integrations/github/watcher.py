"""gh-webhook-watcher（用户的监听服务）的客户端。

**用 SSE 订阅**（服务 README 的推荐用法）：`/api/stream` 每收到一条事件就推一帧，
带 `id:`（就是 seq）、15 秒一行注释心跳，断线重连时客户端可以带 `Last-Event-ID` 让服务把漏掉的补发。

一个真实差异值得记：**轮询响应里有 `since_evicted`，SSE 没有**。断线太久、环形缓冲把旧事件
挤掉时，SSE 只会「从还有的地方继续」，**不会告诉你中间漏了**。所以这里的做法是：
进流之前用一次 `/api/events`（只取一个字段）做区间检查，确认游标还在缓冲里；不在就去做全量对账。

这个服务**不是历史来源**：缓冲只留最近 `GHW_EVENT_BUFFER` 条（默认 500），停机期间的投递也不会补。
所以「历史」永远由 GitHub API 提供（`releases.py`），这里只负责「刚刚发生了什么」。
"""

from __future__ import annotations

import json
import logging
from collections.abc import AsyncIterator, Iterable, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol
from urllib.parse import unquote

from .client import Transport
from .releases import Release

EVENTS_PATH = "/api/events"
STREAM_PATH = "/api/stream"
DEFAULT_LIMIT = 100
RELEASE_KIND = "Release"
DEFAULT_RETRY_MS = 3000
MAX_RETRY_MS = 60_000
TAG_URL_MARKER = "/releases/tag/"


def tag_from_url(url: str) -> str | None:
    """从 release 链接里取出 tag：release 的 ``html_url`` 一定以 ``/releases/tag/<tag>`` 结尾。

    监听服务没带 payload 时，这是我们唯一能拿到 tag 的地方（事件里的 ``title`` 是人写的版本名）。
    """
    if not url or TAG_URL_MARKER not in url:
        return None
    tail = url.split(TAG_URL_MARKER, 1)[1].split("?", 1)[0].strip("/")
    return unquote(tail) or None


@dataclass(frozen=True)
class WatcherEvent:
    """监听服务规范化后的一条事件。``raw`` 是完整事件（含 ``payload`` 时才有 release 正文）。"""

    seq: int
    kind: str
    full_name: str
    action: str
    title: str
    url: str
    received_at: str
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class WatcherPage:
    events: tuple[WatcherEvent, ...]
    cursor: int
    latest_seq: int
    more: bool
    since_evicted: bool


class WatcherError(RuntimeError):
    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


class WatcherClient:
    """轮询面。这里只用它做一件事：**区间检查**（拿 `since_evicted`）。"""

    def __init__(
        self,
        base_url: str,
        transport: Transport,
        *,
        token: str | None = None,
        user_agent: str = "SyncaindDiscordBot",
        logger: logging.Logger | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._transport = transport
        self._token = token or None
        self._user_agent = user_agent
        self._logger = logger or logging.getLogger("bot.github_feed")

    @property
    def base_url(self) -> str:
        return self._base

    def headers(self) -> dict[str, str]:
        headers = {"Accept": "application/json", "User-Agent": self._user_agent}
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def fetch(
        self,
        since: int,
        *,
        limit: int = DEFAULT_LIMIT,
        kind: str | None = RELEASE_KIND,
        repo: str | None = None,
    ) -> WatcherPage:
        query = [f"since={int(since)}", f"limit={int(limit)}"]
        if kind:
            query.append(f"kind={kind}")
        if repo:
            query.append(f"repo={repo}")
        url = f"{self._base}{EVENTS_PATH}?{'&'.join(query)}"
        try:
            payload = await self._transport.get_json(url, headers=self.headers())
        except Exception as exc:
            raise _as_watcher_error(exc) from exc
        if not isinstance(payload, dict):
            raise WatcherError("监听服务返回了非预期的结构")

        events = tuple(_to_event(item) for item in payload.get("events") or [] if isinstance(item, dict))
        return WatcherPage(
            events=events,
            cursor=int(payload.get("cursor") or since),
            latest_seq=int(payload.get("latest_seq") or 0),
            more=bool(payload.get("more")),
            since_evicted=bool(payload.get("since_evicted")),
        )


@dataclass(frozen=True)
class SseFrame:
    """一个 SSE 帧。``data`` 已经是多行 `data:` 合并后的结果。"""

    data: str
    event_id: str | None = None
    event: str | None = None
    retry_ms: int | None = None


def parse_sse_frame(lines: Sequence[str]) -> SseFrame | None:
    """解析一个 SSE 帧（调用方已按空行切好）。

    纯注释（`: heartbeat ...`）返回 ``None`` —— 心跳不是数据，别当成事件推给上层。
    """
    event_id: str | None = None
    event: str | None = None
    retry_ms: int | None = None
    data: list[str] = []

    for raw in lines:
        line = raw.rstrip("\r\n")
        if not line or line.startswith(":"):
            continue
        field_name, separator, value = line.partition(":")
        if separator and value.startswith(" "):
            value = value[1:]
        if field_name == "data":
            data.append(value)
        elif field_name == "id":
            event_id = value
        elif field_name == "event":
            event = value
        elif field_name == "retry":
            retry_ms = int(value) if value.isdigit() else None

    if not data and event_id is None and retry_ms is None:
        return None
    return SseFrame(data="\n".join(data), event_id=event_id, event=event, retry_ms=retry_ms)


async def iter_frames(lines: AsyncIterator[str]) -> AsyncIterator[SseFrame]:
    """把逐行的响应体切成帧。流结束时若还有残留，也交出去（服务端可能没补尾空行）。"""
    block: list[str] = []
    async for raw in lines:
        if raw.strip() == "":
            frame = parse_sse_frame(block)
            block = []
            if frame is not None:
                yield frame
            continue
        block.append(raw)
    frame = parse_sse_frame(block)
    if frame is not None:
        yield frame


class WatcherStream:
    """SSE 订阅面。``session`` 可注入（测试用假会话，不需要网络）。"""

    def __init__(
        self,
        base_url: str,
        *,
        token: str | None = None,
        user_agent: str = "SyncaindDiscordBot",
        session: Any | None = None,
        logger: logging.Logger | None = None,
    ) -> None:
        self._base = base_url.rstrip("/")
        self._token = token or None
        self._user_agent = user_agent
        self._session = session
        self._logger = logger or logging.getLogger("bot.github_feed")
        # 服务端给的 retry 建议（毫秒），重连退避用它当基准。
        self.retry_ms = DEFAULT_RETRY_MS

    def headers(self, since: int) -> dict[str, str]:
        headers = {
            "Accept": "text/event-stream",
            "User-Agent": self._user_agent,
            "Cache-Control": "no-cache",
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        if since > 0:
            # 断线续传：服务会把它还留着的那部分补发。
            headers["Last-Event-ID"] = str(int(since))
        return headers

    def stream_url(self, *, kind: str | None = RELEASE_KIND, repo: str | None = None) -> str:
        query = []
        if kind:
            query.append(f"kind={kind}")
        if repo:
            query.append(f"repo={repo}")
        suffix = f"?{'&'.join(query)}" if query else ""
        return f"{self._base}{STREAM_PATH}{suffix}"

    async def events(
        self,
        since: int,
        *,
        kind: str | None = RELEASE_KIND,
        repo: str | None = None,
    ) -> AsyncIterator[WatcherEvent]:
        """连上去、逐条 yield；服务端断开就结束这个生成器，由调用方决定何时重连。"""
        session = self._session
        owns_session = session is None
        if session is None:
            import aiohttp

            session = aiohttp.ClientSession()
        try:
            url = self.stream_url(kind=kind, repo=repo)
            async with session.get(url, headers=self.headers(since)) as response:
                if getattr(response, "status", 200) != 200:
                    raise WatcherError(f"监听服务拒绝了订阅（HTTP {response.status}）", status=response.status)
                async for frame in iter_frames(response.content):
                    if frame.retry_ms:
                        self.retry_ms = max(1000, min(frame.retry_ms, MAX_RETRY_MS))
                    if not frame.data:
                        continue
                    try:
                        payload = json.loads(frame.data)
                    except ValueError:
                        self._logger.warning("监听服务推来一帧不是 JSON，跳过")
                        continue
                    if isinstance(payload, dict):
                        yield _to_event(payload)
        finally:
            if owns_session:
                await session.close()


def release_from_event(event: WatcherEvent) -> Release | None:
    """把一条事件变成 :class:`Release`。

    只有监听服务开了 ``GHW_INCLUDE_PAYLOAD`` 时事件里才带 GitHub 原始 payload（含 release 正文）；
    没开就返回 ``None``，由调用方回落到 GitHub API 按 tag 取 —— **两条路都要能用**。
    """
    payload = event.raw.get("payload")
    if not isinstance(payload, dict):
        return None
    release = payload.get("release")
    if not isinstance(release, dict) or not release:
        return None
    if bool(release.get("draft")):
        return None

    release_id = release.get("id")
    tag = str(release.get("tag_name") or "").strip()
    if release_id is None or not tag:
        return None

    return Release(
        repo=event.full_name,
        release_id=int(release_id),
        tag=tag,
        name=str(release.get("name") or ""),
        body=str(release.get("body") or ""),
        html_url=str(release.get("html_url") or event.url or ""),
        published_at=str(release.get("published_at") or release.get("created_at") or event.received_at or ""),
        prerelease=bool(release.get("prerelease")),
        draft=False,
    )


def _to_event(item: dict[str, Any]) -> WatcherEvent:
    return WatcherEvent(
        seq=int(item.get("seq") or 0),
        kind=str(item.get("kind") or ""),
        full_name=str(item.get("full_name") or ""),
        action=str(item.get("action") or ""),
        title=str(item.get("title") or ""),
        url=str(item.get("url") or ""),
        received_at=str(item.get("received_at") or ""),
        raw=item,
    )


def _as_watcher_error(exc: Exception) -> WatcherError:
    status = getattr(exc, "status", None)
    if status == 401:
        return WatcherError("监听服务拒绝了这次请求（令牌不对或没配）", status=401)
    if status is not None:
        return WatcherError(f"监听服务返回 HTTP {status}", status=status)
    return WatcherError(f"连不上监听服务：{type(exc).__name__}")


class WatcherSource(Protocol):
    """轮询面。抽出来是为了测试能塞假的。"""

    async def fetch(self, since: int, **kwargs: Any) -> WatcherPage: ...


class WatcherStreamSource(Protocol):
    """SSE 面。"""

    retry_ms: int

    def events(self, since: int, **kwargs: Any) -> AsyncIterator[WatcherEvent]: ...


def frame_lines(raw_lines: Iterable[str]) -> list[str]:
    """小工具：把一段文本切成行（测试里方便造帧）。"""
    return [line + "\n" for line in raw_lines]
