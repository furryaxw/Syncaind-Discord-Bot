"""OAuth 的 ``state`` 管理：把「浏览器回调」认回「是哪个 Discord 用户在授权」。

``state`` 是 OAuth 里唯一防 CSRF 的东西，所以这里只管三件事：
**随机生成**、**一次性**、**会过期**。

存在内存里而不是数据库：授权流程是分钟级的，机器人重启后让用户重来一次即可——
为此在库里多一张表、多一套清理逻辑不划算。这一点要在文案里说清楚（重试成本很低）。
"""

from __future__ import annotations

import secrets
import time
from dataclasses import dataclass

DEFAULT_TTL_SECONDS = 600
TOKEN_BYTES = 32


@dataclass(frozen=True)
class PendingLink:
    state: str
    guild_id: int
    discord_user_id: int
    created_at: float
    expires_at: float


class StateStore:
    def __init__(self, *, ttl_seconds: int = DEFAULT_TTL_SECONDS, clock=time.monotonic) -> None:
        self._ttl = ttl_seconds
        self._clock = clock
        self._pending: dict[str, PendingLink] = {}

    def __len__(self) -> int:
        return len(self._pending)

    def create(self, *, guild_id: int, discord_user_id: int) -> PendingLink:
        self._purge()
        now = self._clock()
        link = PendingLink(
            state=secrets.token_urlsafe(TOKEN_BYTES),
            guild_id=guild_id,
            discord_user_id=discord_user_id,
            created_at=now,
            expires_at=now + self._ttl,
        )
        self._pending[link.state] = link
        return link

    def consume(self, state: str) -> PendingLink | None:
        """取用并**立刻作废**。查不到、已用过或已过期都返回 ``None``。"""
        self._purge()
        link = self._pending.pop(state, None)
        if link is None:
            return None
        if link.expires_at <= self._clock():
            return None
        return link

    def _purge(self) -> None:
        now = self._clock()
        for state in [state for state, link in self._pending.items() if link.expires_at <= now]:
            del self._pending[state]
