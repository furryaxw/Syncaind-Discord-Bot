"""moderation 的数据部分：处罚记录与警告累计。

这里是**纯数据逻辑**，不碰 Discord API，因此可以离线测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

import aiosqlite

from bot.core.clock import utcnow_iso
from bot.core.database import Database
from bot.core.store import GuildSettingsStore


@dataclass(frozen=True)
class RecordedAction:
    """一条已经落库的处罚记录。"""

    case_number: int
    action_id: int
    created_at: str


def should_escalate(*, active_warnings: int, threshold: int) -> bool:
    """有效警告数达到阈值整数倍时升级。

    ``threshold <= 0`` 表示关闭自动升级。用取模而不是 ``==``，这样「第 3、6、9 次」都会升级，
    不会在第 4 次警告时悄悄漏掉。
    """
    if threshold <= 0 or active_warnings <= 0:
        return False
    return active_warnings % threshold == 0


class ModerationService:
    def __init__(
        self,
        db: Database,
        settings_store: GuildSettingsStore,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._db = db
        self._settings_store = settings_store
        self._logger = logger or logging.getLogger("bot.moderation")

    async def record_action(
        self,
        guild_id: int,
        *,
        action: str,
        target_id: int,
        moderator_id: int,
        reason: str | None = None,
        duration_seconds: int | None = None,
        automated: bool = False,
    ) -> RecordedAction:
        """写入一条处罚记录并分配 case 编号。"""
        async with self._db.transaction() as connection:
            return await self._insert_action(
                connection,
                guild_id=guild_id,
                action=action,
                target_id=target_id,
                moderator_id=moderator_id,
                reason=reason,
                duration_seconds=duration_seconds,
                automated=automated,
            )

    async def add_warning(
        self,
        guild_id: int,
        *,
        target_id: int,
        moderator_id: int,
        reason: str | None = None,
    ) -> tuple[RecordedAction, int]:
        """记录警告并返回 (处罚记录, 该成员当前有效警告数)。

        警告与计数在同一个事务里完成，避免并发下数错。
        """
        async with self._db.transaction() as connection:
            record = await self._insert_action(
                connection,
                guild_id=guild_id,
                action="warn",
                target_id=target_id,
                moderator_id=moderator_id,
                reason=reason,
                duration_seconds=None,
                automated=False,
            )
            await connection.execute(
                """
                INSERT INTO warnings (guild_id, target_id, moderator_id, reason, action_id, active, created_at)
                VALUES (?, ?, ?, ?, ?, 1, ?)
                """,
                (guild_id, target_id, moderator_id, reason, record.action_id, record.created_at),
            )
            cursor = await connection.execute(
                """
                SELECT COUNT(*) AS total FROM warnings
                WHERE guild_id = ? AND target_id = ? AND active = 1
                """,
                (guild_id, target_id),
            )
            row = await cursor.fetchone()
            active = int(row["total"]) if row is not None else 0
        self._logger.info(
            "记录警告：guild=%s target=%s case=%s 有效警告=%s",
            guild_id,
            target_id,
            record.case_number,
            active,
        )
        return record, active

    async def active_warning_count(self, guild_id: int, target_id: int) -> int:
        row = await self._db.fetchone(
            """
            SELECT COUNT(*) AS total FROM warnings
            WHERE guild_id = ? AND target_id = ? AND active = 1
            """,
            (guild_id, target_id),
        )
        return int(row["total"]) if row is not None else 0

    async def _insert_action(
        self,
        connection: aiosqlite.Connection,
        *,
        guild_id: int,
        action: str,
        target_id: int,
        moderator_id: int,
        reason: str | None,
        duration_seconds: int | None,
        automated: bool,
    ) -> RecordedAction:
        cursor = await connection.execute(
            "SELECT COALESCE(MAX(case_number), 0) + 1 AS next FROM moderation_actions WHERE guild_id = ?",
            (guild_id,),
        )
        row = await cursor.fetchone()
        case_number = int(row["next"]) if row is not None else 1
        created_at = utcnow_iso()

        cursor = await connection.execute(
            """
            INSERT INTO moderation_actions (
                guild_id, case_number, action, target_id, moderator_id,
                reason, duration_seconds, automated, created_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                case_number,
                action,
                target_id,
                moderator_id,
                reason,
                duration_seconds,
                int(automated),
                created_at,
            ),
        )
        return RecordedAction(
            case_number=case_number,
            action_id=int(cursor.lastrowid or 0),
            created_at=created_at,
        )


__all__: list[str] = [
    "ModerationService",
    "RecordedAction",
    "should_escalate",
]
