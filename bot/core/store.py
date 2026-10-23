"""数据访问：每服务器的设置与模块开关状态。"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, replace

from .clock import utcnow_iso
from .database import Database


@dataclass(frozen=True)
class GuildSettings:
    """单个服务器的管理设置。"""

    guild_id: int
    locale: str | None = None
    updated_at: str = ""

    @classmethod
    def default(cls, guild_id: int) -> GuildSettings:
        return cls(guild_id=guild_id)


class GuildSettingsStore:
    """读写 ``guild_settings``。行不存在时返回默认值，不写库。"""

    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.settings")
        self._lock = asyncio.Lock()

    async def get(self, guild_id: int) -> GuildSettings:
        row = await self._db.fetchone("SELECT * FROM guild_settings WHERE guild_id = ?", (guild_id,))
        if row is None:
            return GuildSettings.default(guild_id)
        return GuildSettings(
            guild_id=int(row["guild_id"]),
            locale=row["locale"],
            updated_at=str(row["updated_at"]),
        )

    async def update(self, guild_id: int, **changes: object) -> GuildSettings:
        """局部更新。读改写整段加锁，避免并发覆盖。"""
        unknown = set(changes) - {"locale"}
        if unknown:
            raise ValueError(f"不支持的设置项：{sorted(unknown)}")

        async with self._lock:
            current = await self.get(guild_id)
            updated = replace(current, updated_at=utcnow_iso(), **changes)
            await self._db.execute(
                """
                INSERT INTO guild_settings (guild_id, locale, updated_at)
                VALUES (?, ?, ?)
                ON CONFLICT (guild_id) DO UPDATE SET
                    locale = excluded.locale,
                    updated_at = excluded.updated_at
                """,
                (updated.guild_id, updated.locale, updated.updated_at),
            )
            return updated


class ModuleStateStore:
    """读写 ``module_states``。没有行 = 用模块自己的 default_enabled。"""

    def __init__(self, db: Database) -> None:
        self._db = db

    async def overrides(self, guild_id: int) -> dict[str, bool]:
        rows = await self._db.fetchall("SELECT module_id, enabled FROM module_states WHERE guild_id = ?", (guild_id,))
        return {str(row["module_id"]): bool(row["enabled"]) for row in rows}

    async def set_enabled(self, guild_id: int, module_id: str, enabled: bool) -> None:
        await self._db.execute(
            """
            INSERT INTO module_states (guild_id, module_id, enabled, updated_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, module_id) DO UPDATE SET
                enabled = excluded.enabled,
                updated_at = excluded.updated_at
            """,
            (guild_id, module_id, int(enabled), utcnow_iso()),
        )
