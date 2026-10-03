"""reaction_roles 的数据部分：表情 ↔ 角色映射。

映射**必须落库**：反应事件是原始事件（raw），重启后进程里什么都没有，
每一次反应都要靠数据库回答「这个表情对应哪个角色」。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.core.clock import utcnow_iso
from bot.core.database import Database


@dataclass(frozen=True)
class ReactionRoleMapping:
    guild_id: int
    message_id: int
    channel_id: int
    emoji: str
    role_id: int
    created_by: int
    created_at: str

    @property
    def jump_url(self) -> str:
        return f"https://discord.com/channels/{self.guild_id}/{self.channel_id}/{self.message_id}"


class ReactionRoleService:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.reaction_roles")

    async def add(
        self,
        guild_id: int,
        *,
        message_id: int,
        channel_id: int,
        emoji: str,
        role_id: int,
        created_by: int,
    ) -> None:
        """建立映射。同一个表情重复添加就是**覆盖**——主键决定了这点。"""
        await self._db.execute(
            """
            INSERT INTO reaction_roles (guild_id, message_id, channel_id, emoji, role_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, message_id, emoji) DO UPDATE SET
                role_id = excluded.role_id,
                channel_id = excluded.channel_id,
                created_by = excluded.created_by,
                created_at = excluded.created_at
            """,
            (guild_id, message_id, channel_id, emoji, role_id, created_by, utcnow_iso()),
        )

    async def remove(self, guild_id: int, message_id: int, emoji: str) -> int:
        """删掉一条映射，返回删除的行数（0 表示本来就没有）。"""
        return await self._db.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ? AND emoji = ?",
            (guild_id, message_id, emoji),
        )

    async def clear_message(self, guild_id: int, message_id: int) -> int:
        return await self._db.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND message_id = ?",
            (guild_id, message_id),
        )

    async def remove_all_for_role(self, guild_id: int, role_id: int) -> int:
        """角色被删掉时顺手清掉它的映射，避免留下指向空气的规则。"""
        return await self._db.execute(
            "DELETE FROM reaction_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )

    async def role_id_for(self, guild_id: int, message_id: int, emoji: str) -> int | None:
        """反应事件的热路径：只取一个 role_id。"""
        row = await self._db.fetchone(
            """
            SELECT role_id FROM reaction_roles
            WHERE guild_id = ? AND message_id = ? AND emoji = ?
            """,
            (guild_id, message_id, emoji),
        )
        return int(row["role_id"]) if row is not None else None

    async def for_message(self, guild_id: int, message_id: int) -> list[ReactionRoleMapping]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM reaction_roles
            WHERE guild_id = ? AND message_id = ?
            ORDER BY created_at
            """,
            (guild_id, message_id),
        )
        return [_to_mapping(row) for row in rows]

    async def for_guild(self, guild_id: int) -> list[ReactionRoleMapping]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM reaction_roles
            WHERE guild_id = ?
            ORDER BY message_id, created_at
            """,
            (guild_id,),
        )
        return [_to_mapping(row) for row in rows]


def _to_mapping(row) -> ReactionRoleMapping:
    return ReactionRoleMapping(
        guild_id=int(row["guild_id"]),
        message_id=int(row["message_id"]),
        channel_id=int(row["channel_id"]),
        emoji=str(row["emoji"]),
        role_id=int(row["role_id"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )
