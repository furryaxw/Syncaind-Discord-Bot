"""节点 → 角色 绑定的存储。"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.core.clock import utcnow_iso
from bot.core.database import Database

from .nodes import normalize_pattern


@dataclass(frozen=True)
class NodeRoleBinding:
    guild_id: int
    node_pattern: str
    role_id: int
    created_by: int
    created_at: str


class NodeRoleBindingStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.access_roles")

    async def bind(self, guild_id: int, *, pattern: str, role_id: int, created_by: int) -> bool:
        """绑一个「模式 → 角色」。已经绑过返回 ``False``（没改动）。"""
        normalized = normalize_pattern(pattern)
        cursor = await self._db.execute(
            """
            INSERT INTO node_role_bindings (guild_id, node_pattern, role_id, created_by, created_at)
            VALUES (?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, node_pattern, role_id) DO NOTHING
            """,
            (guild_id, normalized, role_id, created_by, utcnow_iso()),
        )
        if cursor:
            self._logger.info("绑定权限节点：%s → role %s", normalized, role_id)
        return bool(cursor)

    async def unbind(self, guild_id: int, *, pattern: str, role_id: int | None = None) -> int:
        """解绑。不传 ``role_id`` 就解绑该模式下的所有角色。返回删除的行数。"""
        normalized = normalize_pattern(pattern)
        if role_id is None:
            return await self._db.execute(
                "DELETE FROM node_role_bindings WHERE guild_id = ? AND node_pattern = ?",
                (guild_id, normalized),
            )
        return await self._db.execute(
            "DELETE FROM node_role_bindings WHERE guild_id = ? AND node_pattern = ? AND role_id = ?",
            (guild_id, normalized, role_id),
        )

    async def all(self, guild_id: int) -> list[NodeRoleBinding]:
        rows = await self._db.fetchall(
            "SELECT * FROM node_role_bindings WHERE guild_id = ? ORDER BY node_pattern, role_id",
            (guild_id,),
        )
        return [_to_binding(row) for row in rows]

    async def pairs(self, guild_id: int) -> list[tuple[str, int]]:
        """给判定逻辑用的 (模式, 角色) 列表。"""
        return [(binding.node_pattern, binding.role_id) for binding in await self.all(guild_id)]

    async def bound_role_ids(self, guild_id: int) -> set[int]:
        """绑定表里出现过的所有角色 —— **同步时只允许动这些角色**。"""
        rows = await self._db.fetchall(
            "SELECT DISTINCT role_id FROM node_role_bindings WHERE guild_id = ?",
            (guild_id,),
        )
        return {int(row["role_id"]) for row in rows}


def _to_binding(row) -> NodeRoleBinding:
    return NodeRoleBinding(
        guild_id=int(row["guild_id"]),
        node_pattern=str(row["node_pattern"]),
        role_id=int(row["role_id"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )
