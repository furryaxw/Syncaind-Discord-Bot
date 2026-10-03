"""从 GitHub 读 release。这一层只认 HTTP，不认 Discord，所以能完全离线测。

``ReleaseSource`` 是一个**接缝**：现在只有「直连 GitHub」一种实现；
将来换成监听服务（或它提供的 API）时，只要实现同一个协议，上面的绑定/回填/推送全都不用改。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Protocol
from urllib.parse import quote

from .client import API_VERSION, Transport

RELEASES_URL = "https://api.github.com/repos/{repo}/releases"
PER_PAGE = 100
# 回填上限：再多的历史也没有意义（而且会把频道刷爆）。
MAX_RELEASES = 200


@dataclass(frozen=True)
class Release:
    """一条 release。``body`` 是 GitHub 上的 release notes 原文（markdown）。"""

    repo: str
    release_id: int
    tag: str
    name: str
    body: str
    html_url: str
    published_at: str
    prerelease: bool = False
    draft: bool = False

    @property
    def title(self) -> str:
        """展示用标题：有 release 名就用它，否则用 tag。"""
        return self.name.strip() or self.tag


class ReleaseSourceError(RuntimeError):
    """取 release 失败。``status`` 是 HTTP 状态码（取不到就是 None）。"""

    def __init__(self, message: str, *, status: int | None = None, repo: str = "") -> None:
        super().__init__(message)
        self.status = status
        self.repo = repo


class ReleaseSource(Protocol):
    async def list_releases(self, repo: str, *, limit: int = MAX_RELEASES) -> list[Release]:
        """列出某个仓库的 release，**从新到旧**（GitHub 的顺序）。"""
        ...

    async def get_release(self, repo: str, tag: str) -> Release | None:
        """按 tag 取一条 release，取不到返回 ``None``。"""
        ...


class GitHubReleaseSource:
    def __init__(
        self,
        transport: Transport,
        *,
        token: str | None = None,
        user_agent: str = "SyncaindDiscordBot",
        logger: logging.Logger | None = None,
    ) -> None:
        self._transport = transport
        self._token = token or None
        self._user_agent = user_agent
        self._logger = logger or logging.getLogger("bot.github")

    @property
    def authenticated(self) -> bool:
        return self._token is not None

    def _headers(self) -> dict[str, str]:
        headers = {
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": API_VERSION,
            "User-Agent": self._user_agent,
        }
        if self._token:
            headers["Authorization"] = f"Bearer {self._token}"
        return headers

    async def list_releases(self, repo: str, *, limit: int = MAX_RELEASES) -> list[Release]:
        collected: list[Release] = []
        page = 1
        while len(collected) < limit:
            url = f"{RELEASES_URL.format(repo=repo)}?per_page={PER_PAGE}&page={page}"
            try:
                payload = await self._transport.get_json(url, headers=self._headers())
            except Exception as exc:  # aiohttp 的异常类型在这里不该泄漏给上层
                raise _as_source_error(exc, repo) from exc
            if not isinstance(payload, list):
                raise ReleaseSourceError("GitHub 返回了非预期的结构", repo=repo)
            if not payload:
                break
            collected.extend(_to_release(repo, item) for item in payload if isinstance(item, dict))
            if len(payload) < PER_PAGE:
                break
            page += 1

        # 草稿对外不可见，也不该推给任何人。
        releases = [release for release in collected if not release.draft]
        return releases[:limit]

    async def get_release(self, repo: str, tag: str) -> Release | None:
        """按 tag 取一条 release。

        监听服务没开 ``GHW_INCLUDE_PAYLOAD`` 时事件里没有正文，就用这条路把 notes 换回来。
        """
        url = f"{RELEASES_URL.format(repo=repo)}/tags/{quote(tag, safe='')}"
        try:
            payload = await self._transport.get_json(url, headers=self._headers())
        except Exception as exc:
            raise _as_source_error(exc, repo) from exc
        if not isinstance(payload, dict):
            return None
        return _to_release(repo, payload)


def _to_release(repo: str, item: dict[str, Any]) -> Release:
    return Release(
        repo=repo,
        release_id=int(item.get("id") or 0),
        tag=str(item.get("tag_name") or ""),
        name=str(item.get("name") or ""),
        body=str(item.get("body") or ""),
        html_url=str(item.get("html_url") or ""),
        published_at=str(item.get("published_at") or item.get("created_at") or ""),
        prerelease=bool(item.get("prerelease")),
        draft=bool(item.get("draft")),
    )


def _as_source_error(exc: Exception, repo: str) -> ReleaseSourceError:
    status = getattr(exc, "status", None)
    if status == 404:
        return ReleaseSourceError(f"仓库 {repo} 不存在或不可见", status=404, repo=repo)
    if status == 403:
        return ReleaseSourceError("GitHub 拒绝了请求（多半是限流）", status=403, repo=repo)
    return ReleaseSourceError(f"请求 GitHub 失败：{type(exc).__name__}", status=status, repo=repo)
