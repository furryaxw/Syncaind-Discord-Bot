"""激活码的发放记录与**发码活动**（drop）。

* 发放记录**不存明文码**（码从服务端 take 出来就直接私信出去）：
  它存在的理由是 ①「一人一批一枚」这条规则由我们执行 ②私信失败时要知道退哪一枚。
* 活动编号是**短随机**的（6 位、字母表去掉易混字符），便于人念和手打。
"""

from __future__ import annotations

import logging
import secrets
from dataclasses import dataclass
from sqlite3 import IntegrityError
from typing import Any

from bot.core.clock import utcnow_iso
from bot.core.database import Database


@dataclass(frozen=True)
class KeyDelivery:
    guild_id: int
    batch_id: str
    discord_user_id: int
    key_id: str
    key_prefix: str
    delivered_at: str


class KeyDeliveryStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.access_keys")

    async def claim(self, guild_id: int, *, batch_id: str, discord_user_id: int, key_id: str, key_prefix: str) -> bool:
        """登记一次发放。**已经领过就返回 ``False``**（主键挡住并发重复领取）。"""
        cursor = await self._db.execute(
            """
            INSERT INTO key_deliveries (guild_id, batch_id, discord_user_id, key_id, key_prefix, delivered_at)
            VALUES (?, ?, ?, ?, ?, ?)
            ON CONFLICT (guild_id, batch_id, discord_user_id) DO NOTHING
            """,
            (guild_id, batch_id, discord_user_id, key_id, key_prefix, utcnow_iso()),
        )
        return bool(cursor)

    async def already_claimed(self, guild_id: int, *, batch_id: str, discord_user_id: int) -> bool:
        row = await self._db.fetchone(
            "SELECT 1 FROM key_deliveries WHERE guild_id = ? AND batch_id = ? AND discord_user_id = ?",
            (guild_id, batch_id, discord_user_id),
        )
        return row is not None

    async def forget(self, guild_id: int, *, batch_id: str, discord_user_id: int) -> int:
        """退码时把记录一并撤掉 —— 否则当事人再也领不了这一批。"""
        return await self._db.execute(
            "DELETE FROM key_deliveries WHERE guild_id = ? AND batch_id = ? AND discord_user_id = ?",
            (guild_id, batch_id, discord_user_id),
        )

    async def for_batch(self, guild_id: int, batch_id: str) -> list[KeyDelivery]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM key_deliveries
             WHERE guild_id = ? AND batch_id = ?
             ORDER BY delivered_at DESC
            """,
            (guild_id, batch_id),
        )
        return [_to_delivery(row) for row in rows]

    async def count(self, guild_id: int, batch_id: str) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS total FROM key_deliveries WHERE guild_id = ? AND batch_id = ?",
            (guild_id, batch_id),
        )
        return int(row["total"]) if row is not None else 0


def _to_delivery(row) -> KeyDelivery:
    return KeyDelivery(
        guild_id=int(row["guild_id"]),
        batch_id=str(row["batch_id"]),
        discord_user_id=int(row["discord_user_id"]),
        key_id=str(row["key_id"]),
        key_prefix=str(row["key_prefix"]),
        delivered_at=str(row["delivered_at"]),
    )


RAFFLE = "raffle"
FCFS = "fcfs"
OPEN = "open"
DRAWN = "drawn"
CLOSED = "closed"
CANCELLED = "cancelled"

# 活动编号：短随机，便于人念、手打。字母表与 SMAS 的激活码一致 ——
# 去掉 I/L/O/U 这些容易看错或读错的字符。
DROP_ID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"
DROP_ID_LENGTH = 6
DROP_ID_ATTEMPTS = 5


def new_drop_id() -> str:
    """生成一个活动编号（6 位，32^6 ≈ 10 亿，撞了由调用方重试）。"""
    return "".join(secrets.choice(DROP_ID_ALPHABET) for _ in range(DROP_ID_LENGTH))


@dataclass(frozen=True)
class KeyDrop:
    drop_id: str
    guild_id: int
    batch_id: str
    team_id: str
    channel_id: int
    message_id: int | None
    mode: str
    key_count: int
    role_id: int | None
    deny_role_id: int | None
    closes_at: str | None
    delivered: int
    status: str
    created_by: int
    created_at: str

    @property
    def is_raffle(self) -> bool:
        return self.mode == RAFFLE


class KeyDropStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.access_keys")

    async def create(
        self,
        guild_id: int,
        *,
        batch_id: str,
        team_id: str,
        channel_id: int,
        mode: str,
        key_count: int,
        role_id: int | None,
        deny_role_id: int | None,
        closes_at: str | None,
        created_by: int,
    ) -> KeyDrop:
        """建一次活动。编号是**短随机**的，撞了重试（唯一键会拦住）。"""
        for _attempt in range(DROP_ID_ATTEMPTS):
            drop_id = new_drop_id()
            try:
                await self._db.execute(
                    """
                    INSERT INTO key_drops
                        (drop_id, guild_id, batch_id, team_id, channel_id, mode, key_count, role_id,
                         deny_role_id, closes_at, status, created_by, created_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        drop_id,
                        guild_id,
                        batch_id,
                        team_id,
                        channel_id,
                        mode,
                        key_count,
                        role_id,
                        deny_role_id,
                        closes_at,
                        OPEN,
                        created_by,
                        utcnow_iso(),
                    ),
                )
            except IntegrityError:
                self._logger.warning("活动编号撞了，重试：%s", drop_id)
                continue

            drop = await self.get(drop_id)
            assert drop is not None
            self._logger.info("开了一次发码活动：id=%s mode=%s batch=%s count=%d", drop_id, mode, batch_id, key_count)
            return drop

        raise RuntimeError("连续几次都没能生成不冲突的活动编号")

    async def get(self, drop_id: str) -> KeyDrop | None:
        row = await self._db.fetchone("SELECT * FROM key_drops WHERE drop_id = ?", (drop_id,))
        return _to_drop(row) if row is not None else None

    async def get_by_message(self, message_id: int) -> KeyDrop | None:
        """按消息 id 反查 —— 按钮点击就走这条路（见 cog 里「状态不存在 view 里」那条）。"""
        row = await self._db.fetchone("SELECT * FROM key_drops WHERE message_id = ?", (message_id,))
        return _to_drop(row) if row is not None else None

    async def set_message(self, drop_id: str, message_id: int) -> None:
        await self._db.execute("UPDATE key_drops SET message_id = ? WHERE drop_id = ?", (message_id, drop_id))

    async def set_status(self, drop_id: str, status: str) -> None:
        await self._db.execute("UPDATE key_drops SET status = ? WHERE drop_id = ?", (status, drop_id))

    async def due(self, now_iso: str) -> list[KeyDrop]:
        """到点还没开奖的抽奖活动（后台任务据此开奖；跨重启也不会漏）。"""
        rows = await self._db.fetchall(
            """
            SELECT * FROM key_drops
             WHERE status = ? AND mode = ? AND closes_at IS NOT NULL AND closes_at <= ?
             ORDER BY closes_at
            """,
            (OPEN, RAFFLE, now_iso),
        )
        return [_to_drop(row) for row in rows]

    async def open_in_guild(self, guild_id: int) -> list[KeyDrop]:
        rows = await self._db.fetchall(
            "SELECT * FROM key_drops WHERE guild_id = ? AND status = ? ORDER BY created_at DESC",
            (guild_id, OPEN),
        )
        return [_to_drop(row) for row in rows]

    async def take_slot(self, drop_id: str) -> bool:
        """先到先得的名额：**一条 SQL 里自增并封顶**，所以并发点击不会超发。

        返回 ``False`` 表示名额已经满了。
        """
        cursor = await self._db.execute(
            "UPDATE key_drops SET delivered = delivered + 1 WHERE drop_id = ? AND delivered < key_count",
            (drop_id,),
        )
        return bool(cursor)

    async def give_back_slot(self, drop_id: str) -> None:
        """取到名额但码没发出去（私信失败）时把名额还回去。"""
        await self._db.execute(
            "UPDATE key_drops SET delivered = MAX(delivered - 1, 0) WHERE drop_id = ?",
            (drop_id,),
        )

    async def enter(self, drop_id: str, discord_user_id: int) -> bool:
        """报名（抽奖模式）。已经报过返回 ``False``。"""
        cursor = await self._db.execute(
            """
            INSERT INTO key_drop_entries (drop_id, discord_user_id, entered_at)
            VALUES (?, ?, ?)
            ON CONFLICT (drop_id, discord_user_id) DO NOTHING
            """,
            (drop_id, discord_user_id, utcnow_iso()),
        )
        return bool(cursor)

    async def entries(self, drop_id: str) -> list[int]:
        rows = await self._db.fetchall(
            "SELECT discord_user_id FROM key_drop_entries WHERE drop_id = ? ORDER BY entered_at",
            (drop_id,),
        )
        return [int(row["discord_user_id"]) for row in rows]

    async def entry_count(self, drop_id: str) -> int:
        row = await self._db.fetchone("SELECT COUNT(*) AS total FROM key_drop_entries WHERE drop_id = ?", (drop_id,))
        return int(row["total"]) if row is not None else 0


def _to_drop(row) -> KeyDrop:
    raw_message = row["message_id"]
    raw_role = row["role_id"]
    raw_deny_role = row["deny_role_id"]
    raw_closes = row["closes_at"]
    return KeyDrop(
        drop_id=str(row["drop_id"]),
        guild_id=int(row["guild_id"]),
        batch_id=str(row["batch_id"]),
        team_id=str(row["team_id"]),
        channel_id=int(row["channel_id"]),
        message_id=int(raw_message) if raw_message is not None else None,
        mode=str(row["mode"]),
        key_count=int(row["key_count"]),
        role_id=int(raw_role) if raw_role is not None else None,
        deny_role_id=int(raw_deny_role) if raw_deny_role is not None else None,
        closes_at=str(raw_closes) if raw_closes is not None else None,
        delivered=int(row["delivered"]),
        status=str(row["status"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


class KeyDeniedRoleStore:
    """发码黑名单：持有这些角色的人不能领取/参与。

    **服务器级**规则：黑名单压过活动上的资格设置 —— 先排除，再看资格。
    只对某一次活动生效的排除记在活动行上（``KeyDrop.deny_role_id``），这一层只管服务器级的名单。
    """

    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.access_keys")

    async def add(self, guild_id: int, role_id: int, *, created_by: int) -> bool:
        cursor = await self._db.execute(
            """
            INSERT INTO key_denied_roles (guild_id, role_id, created_by, created_at)
            VALUES (?, ?, ?, ?)
            ON CONFLICT (guild_id, role_id) DO NOTHING
            """,
            (guild_id, role_id, created_by, utcnow_iso()),
        )
        if cursor:
            self._logger.info("发码黑名单 +role %s", role_id)
        return bool(cursor)

    async def remove(self, guild_id: int, role_id: int) -> int:
        removed = await self._db.execute(
            "DELETE FROM key_denied_roles WHERE guild_id = ? AND role_id = ?",
            (guild_id, role_id),
        )
        if removed:
            self._logger.info("发码黑名单 -role %s", role_id)
        return removed

    async def all(self, guild_id: int) -> set[int]:
        rows = await self._db.fetchall("SELECT role_id FROM key_denied_roles WHERE guild_id = ?", (guild_id,))
        return {int(row["role_id"]) for row in rows}

    async def blocks(self, guild_id: int, role_ids: Any) -> bool:
        """这个人（按他的角色集合）是否被黑名单挡住。"""
        denied = await self.all(guild_id)
        return bool(denied.intersection(int(item) for item in role_ids))
