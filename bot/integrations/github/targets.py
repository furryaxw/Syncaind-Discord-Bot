"""release 推送目标的存储：一个仓库 → 一个 Discord 帖子/频道，外加推送游标。

游标用 GitHub 的 release id：它**单调递增**，比 published_at 稳（release 可以被编辑、时间会变，
id 不会）。所以「id 大于游标的都要推」是一条不会漏、也不会重推的规则。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.core.clock import utcnow_iso
from bot.core.database import Database


@dataclass(frozen=True)
class ReleaseTarget:
    guild_id: int
    repo: str
    channel_id: int
    cursor_release_id: int | None
    created_by: int
    created_at: str


class ReleaseTargetStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.github")

    async def add(
        self,
        guild_id: int,
        *,
        repo: str,
        channel_id: int,
        cursor_release_id: int | None,
        created_by: int,
    ) -> ReleaseTarget:
        """绑定（或改绑）一个仓库。同一仓库再次绑定会覆盖目标并把游标重置为给的值。"""
        await self._db.execute(
            """
            INSERT INTO release_targets (guild_id, repo, channel_id, cursor_release_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, repo) DO UPDATE SET
                channel_id = excluded.channel_id,
                cursor_release_id = excluded.cursor_release_id,
                created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (guild_id, repo, channel_id, cursor_release_id, created_by, utcnow_iso()),
        )
        self._logger.info("绑定 release 推送：%s → channel %s", repo, channel_id)
        target = await self.get(guild_id, repo)
        assert target is not None  # 刚写进去的
        return target

    async def get(self, guild_id: int, repo: str) -> ReleaseTarget | None:
        row = await self._db.fetchone(
            "SELECT * FROM release_targets WHERE guild_id = ? AND repo = ?",
            (guild_id, repo),
        )
        return _to_target(row) if row is not None else None

    async def all(self, guild_id: int) -> list[ReleaseTarget]:
        rows = await self._db.fetchall(
            "SELECT * FROM release_targets WHERE guild_id = ? ORDER BY repo",
            (guild_id,),
        )
        return [_to_target(row) for row in rows]

    async def remove(self, guild_id: int, repo: str) -> int:
        return await self._db.execute(
            "DELETE FROM release_targets WHERE guild_id = ? AND repo = ?",
            (guild_id, repo),
        )

    async def set_cursor(self, guild_id: int, repo: str, release_id: int) -> None:
        """把游标往前推。**只往前**：并发或重放时退回去会导致重推。"""
        await self._db.execute(
            """
            UPDATE release_targets
               SET cursor_release_id = ?
             WHERE guild_id = ? AND repo = ?
               AND (cursor_release_id IS NULL OR cursor_release_id < ?)
            """,
            (release_id, guild_id, repo, release_id),
        )


def _to_target(row) -> ReleaseTarget:
    raw_cursor = row["cursor_release_id"]
    return ReleaseTarget(
        guild_id=int(row["guild_id"]),
        repo=str(row["repo"]),
        channel_id=int(row["channel_id"]),
        cursor_release_id=int(raw_cursor) if raw_cursor is not None else None,
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


class WatcherCursorStore:
    """监听服务的消费游标。**全局一条**：那个服务只有一路事件流。

    ``seq`` 跨重启单调递增，所以游标可以长期存着；这里也**只许前进**，
    退回旧值意味着把已经推过的事件再推一遍。
    """

    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.github_feed")

    async def get(self) -> int:
        row = await self._db.fetchone("SELECT cursor FROM watcher_state WHERE id = 1")
        return int(row["cursor"]) if row is not None else 0

    async def set(self, cursor: int) -> None:
        await self._db.execute(
            """
            INSERT INTO watcher_state (id, cursor, updated_at) VALUES (1, ?, ?)
            ON CONFLICT (id) DO UPDATE SET
                cursor = MAX(watcher_state.cursor, excluded.cursor),
                updated_at = excluded.updated_at
            """,
            (cursor, utcnow_iso()),
        )
