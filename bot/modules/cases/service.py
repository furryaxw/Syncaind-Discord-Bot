"""cases 的数据部分：读处罚记录、撤销它们。

这里是**纯数据逻辑**，不碰 Discord API，因此可以离线测试。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from bot.core.clock import utcnow_iso
from bot.core.database import Database

DEFAULT_HISTORY_LIMIT = 10
MAX_HISTORY_LIMIT = 25

# 处罚动作 → 撤销它在 Discord 侧要做什么。
# 没有对应条目的动作用 "none"：踢掉的人得自己回来、删掉的消息找不回来，
# 这种仍然会被标记为「已撤销」，但它只是记录，不会真的回滚。
UNDO_ACTIONS: dict[str, str] = {
    "ban": "unban",
    "auto_ban": "unban",
    "timeout": "untimeout",
    "auto_timeout": "untimeout",
    "warn": "clear_warning",
}

# 撤销某种动作需要什么权限。比统一要求某个权限更精确：
# 一个有 moderate_members 但没有 ban_members 的人，不该能解封别人。
ACTION_PERMISSIONS: dict[str, str] = {
    "ban": "ban_members",
    "auto_ban": "ban_members",
    "kick": "kick_members",
    "auto_kick": "kick_members",
    "timeout": "moderate_members",
    "auto_timeout": "moderate_members",
    "warn": "moderate_members",
    "purge": "manage_messages",
}


class CaseError(Exception):
    """case 层面的错误。由调用方翻成用户可读文案。"""


class CaseNotFound(CaseError):
    def __init__(self, case_number: int) -> None:
        super().__init__(f"case #{case_number} 不存在")
        self.case_number = case_number


class CaseAlreadyRevoked(CaseError):
    def __init__(self, case_number: int) -> None:
        super().__init__(f"case #{case_number} 已经撤销过了")
        self.case_number = case_number


def undo_plan(action: str) -> str:
    """这个动作撤销时该做什么：``unban`` / ``untimeout`` / ``clear_warning`` / ``none``。"""
    return UNDO_ACTIONS.get(action, "none")


def required_permission(action: str) -> str | None:
    """撤销这个动作需要的权限位名。未知动作用 ``moderate_members`` 兜底。"""
    return ACTION_PERMISSIONS.get(action, "moderate_members")


@dataclass(frozen=True)
class CaseRecord:
    case_number: int
    action: str
    target_id: int
    moderator_id: int
    reason: str | None
    duration_seconds: int | None
    automated: bool
    created_at: str
    revoked_at: str | None
    revoked_by: int | None
    revoke_reason: str | None

    @property
    def is_revoked(self) -> bool:
        return self.revoked_at is not None


class CaseService:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.cases")

    async def history(
        self,
        guild_id: int,
        target_id: int,
        *,
        limit: int = DEFAULT_HISTORY_LIMIT,
    ) -> list[CaseRecord]:
        """某个成员的处罚史，新的在前。"""
        bounded = max(1, min(int(limit), MAX_HISTORY_LIMIT))
        rows = await self._db.fetchall(
            """
            SELECT * FROM moderation_actions
            WHERE guild_id = ? AND target_id = ?
            ORDER BY case_number DESC
            LIMIT ?
            """,
            (guild_id, target_id, bounded),
        )
        return [_to_record(row) for row in rows]

    async def get(self, guild_id: int, case_number: int) -> CaseRecord | None:
        row = await self._db.fetchone(
            "SELECT * FROM moderation_actions WHERE guild_id = ? AND case_number = ?",
            (guild_id, case_number),
        )
        return _to_record(row) if row is not None else None

    async def revoke(
        self,
        guild_id: int,
        case_number: int,
        *,
        moderator_id: int,
        reason: str | None = None,
    ) -> CaseRecord:
        """标记一条处罚为已撤销，并停用它关联的警告。

        真正的 Discord 侧回滚（解封、解除禁言）由调用方在**之前或之后**执行；
        这里只负责把记录状态改对。标记与停用警告在同一个事务里，避免出现
        「case 说撤销了、警告还在累计」这种自相矛盾的状态。
        """
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                "SELECT id, revoked_at FROM moderation_actions WHERE guild_id = ? AND case_number = ?",
                (guild_id, case_number),
            )
            row = await cursor.fetchone()
            if row is None:
                raise CaseNotFound(case_number)
            if row["revoked_at"] is not None:
                raise CaseAlreadyRevoked(case_number)

            await connection.execute(
                """
                UPDATE moderation_actions
                SET revoked_at = ?, revoked_by = ?, revoke_reason = ?
                WHERE id = ?
                """,
                (utcnow_iso(), moderator_id, reason, row["id"]),
            )
            await connection.execute("UPDATE warnings SET active = 0 WHERE action_id = ?", (row["id"],))

        self._logger.info("撤销 case #%s（guild=%s）", case_number, guild_id)
        record = await self.get(guild_id, case_number)
        assert record is not None  # 事务刚写进去的
        return record


def _to_record(row) -> CaseRecord:
    return CaseRecord(
        case_number=int(row["case_number"]),
        action=str(row["action"]),
        target_id=int(row["target_id"]),
        moderator_id=int(row["moderator_id"]),
        reason=row["reason"],
        duration_seconds=row["duration_seconds"],
        automated=bool(row["automated"]),
        created_at=str(row["created_at"]),
        revoked_at=row["revoked_at"],
        revoked_by=row["revoked_by"],
        revoke_reason=row["revoke_reason"],
    )
