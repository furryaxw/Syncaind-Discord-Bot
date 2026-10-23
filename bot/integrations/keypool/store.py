"""本地 key 池的存储层：池、key、活动、入口卡片、报名与领取。

这一层不认识 Discord，只做数据与状态机；命令/面板层负责权限、文案与消息。
"""

from __future__ import annotations

import logging
import secrets
import sqlite3
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any

from bot.core.clock import utcnow_iso
from bot.core.database import Database

AVAILABLE = "available"
RESERVED = "reserved"
DELIVERED = "delivered"
VOID = "void"
KEY_STATUSES = (AVAILABLE, RESERVED, DELIVERED, VOID)

DROP_OPEN = "open"
DROP_SETTLING = "settling"
DROP_CLOSED = "closed"

MODE_RAFFLE = "raffle"
MODE_FCFS = "fcfs"
MODES = (MODE_RAFFLE, MODE_FCFS)

COUNT_FIXED = "fixed"
COUNT_FOLLOW = "follow"
COUNT_MODES = (COUNT_FIXED, COUNT_FOLLOW)

CODE_ALPHABET = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"
CODE_LENGTH = 6
CARD_ACTIVE = "active"
CARD_CLOSED = "closed"
CARD_ANNOUNCEMENT = "announcement"

MAX_RESERVE_ATTEMPTS = 5


class KeyPoolError(RuntimeError):
    """key 池操作的业务错误。"""


class PoolNameTaken(KeyPoolError):
    def __init__(self, name: str) -> None:
        super().__init__(f"池名已存在：{name}")
        self.name = name


class PoolNotEmpty(KeyPoolError):
    def __init__(self) -> None:
        super().__init__("池内还有 key，不能删除")


class PoolInUse(KeyPoolError):
    def __init__(self, codes: Sequence[str]) -> None:
        super().__init__("池仍被活动引用：" + ", ".join(codes))
        self.codes = tuple(codes)


class DropError(KeyPoolError):
    """活动参数或状态不允许该操作。"""


@dataclass(frozen=True)
class KeyPool:
    id: int
    guild_id: int
    name: str
    note: str | None
    enabled: bool
    created_by: int
    created_at: str


@dataclass(frozen=True)
class PoolStats:
    available: int = 0
    reserved: int = 0
    delivered: int = 0
    void: int = 0

    @property
    def total(self) -> int:
        return self.available + self.reserved + self.delivered + self.void


@dataclass(frozen=True)
class KeyRow:
    id: int
    guild_id: int
    pool_id: int
    pool_name: str
    secret: str
    status: str
    added_at: str
    delivered_at: str | None
    delivered_to: int | None
    drop_id: int | None
    voided_at: str | None


@dataclass(frozen=True)
class KeyDrop:
    id: int
    guild_id: int
    code: str
    mode: str
    count_mode: str
    key_count: int | None
    role_id: int | None
    deny_role_id: int | None
    channel_id: int
    closes_at: str | None
    status: str
    delivered: int
    created_by: int
    created_at: str
    retry_until: str | None


@dataclass(frozen=True)
class DropCard:
    id: int
    drop_id: int
    channel_id: int
    message_id: int | None
    status: str
    created_by: int
    created_at: str


@dataclass(frozen=True)
class Reservation:
    key_id: int
    guild_id: int
    pool_id: int
    secret: str
    drop_id: int | None
    user_id: int


class KeyPoolStore:
    def __init__(self, db: Database, *, logger: logging.Logger | None = None) -> None:
        self._db = db
        self._logger = logger or logging.getLogger("bot.keypool")

    # ------------------------------------------------------------------ 池

    async def create_pool(
        self,
        guild_id: int,
        *,
        name: str,
        note: str | None,
        created_by: int,
    ) -> KeyPool:
        try:
            await self._db.execute(
                """
                INSERT INTO key_pools (guild_id, name, note, enabled, created_by, created_at)
                VALUES (?, ?, ?, 1, ?, ?)
                """,
                (guild_id, name, note, created_by, utcnow_iso()),
            )
        except sqlite3.IntegrityError as exc:
            raise PoolNameTaken(name) from exc
        pool = await self.get_pool_by_name(guild_id, name)
        assert pool is not None
        return pool

    async def get_pool(self, guild_id: int, pool_id: int) -> KeyPool | None:
        row = await self._db.fetchone(
            "SELECT * FROM key_pools WHERE guild_id = ? AND id = ?",
            (guild_id, pool_id),
        )
        return _to_pool(row) if row is not None else None

    async def get_pool_by_name(self, guild_id: int, name: str) -> KeyPool | None:
        row = await self._db.fetchone(
            "SELECT * FROM key_pools WHERE guild_id = ? AND name = ?",
            (guild_id, name),
        )
        return _to_pool(row) if row is not None else None

    async def list_pools(self, guild_id: int, *, include_disabled: bool = True) -> list[KeyPool]:
        sql = "SELECT * FROM key_pools WHERE guild_id = ?"
        params: list[Any] = [guild_id]
        if not include_disabled:
            sql += " AND enabled = 1"
        sql += " ORDER BY name"
        rows = await self._db.fetchall(sql, params)
        return [_to_pool(row) for row in rows]

    async def rename_pool(self, guild_id: int, pool_id: int, name: str) -> bool:
        try:
            changed = await self._db.execute(
                "UPDATE key_pools SET name = ? WHERE guild_id = ? AND id = ?",
                (name, guild_id, pool_id),
            )
        except sqlite3.IntegrityError as exc:
            raise PoolNameTaken(name) from exc
        return bool(changed)

    async def set_pool_enabled(self, guild_id: int, pool_id: int, enabled: bool) -> bool:
        changed = await self._db.execute(
            "UPDATE key_pools SET enabled = ? WHERE guild_id = ? AND id = ?",
            (int(enabled), guild_id, pool_id),
        )
        return bool(changed)

    async def delete_pool(self, guild_id: int, pool_id: int) -> None:
        """只有空池、且没有被未结束活动引用时才能删除。"""
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS total FROM key_pool_keys WHERE guild_id = ? AND pool_id = ?",
            (guild_id, pool_id),
        )
        if row is not None and int(row["total"]) > 0:
            raise PoolNotEmpty()
        active = await self.active_drops_for_pool(guild_id, pool_id)
        if active:
            raise PoolInUse([drop.code for drop in active])
        await self._db.execute(
            "DELETE FROM key_pools WHERE guild_id = ? AND id = ?",
            (guild_id, pool_id),
        )

    async def pool_stats(self, guild_id: int, pool_id: int) -> PoolStats:
        rows = await self._db.fetchall(
            """
            SELECT status, COUNT(*) AS total
              FROM key_pool_keys
             WHERE guild_id = ? AND pool_id = ?
             GROUP BY status
            """,
            (guild_id, pool_id),
        )
        counts = {str(row["status"]): int(row["total"]) for row in rows}
        return PoolStats(
            available=counts.get(AVAILABLE, 0),
            reserved=counts.get(RESERVED, 0),
            delivered=counts.get(DELIVERED, 0),
            void=counts.get(VOID, 0),
        )

    # ------------------------------------------------------------------ key

    async def add_keys(self, guild_id: int, pool_id: int, secrets_: Iterable[str]) -> tuple[int, int]:
        """批量导入。返回 (新增, 重复/忽略)。同一服务器内 key 全局唯一。"""
        pool = await self.get_pool(guild_id, pool_id)
        if pool is None:
            raise KeyPoolError("池不存在")

        cleaned: list[str] = []
        for raw in secrets_:
            value = raw.strip()
            if not value:
                continue
            cleaned.append(value)

        inserted = 0
        now = utcnow_iso()
        async with self._db.transaction() as connection:
            for value in cleaned:
                cursor = await connection.execute(
                    """
                    INSERT OR IGNORE INTO key_pool_keys
                        (guild_id, pool_id, secret, status, added_at)
                    VALUES (?, ?, ?, 'available', ?)
                    """,
                    (guild_id, pool_id, value, now),
                )
                inserted += int(cursor.rowcount or 0)
        return inserted, len(cleaned) - inserted

    async def list_keys(self, guild_id: int, pool_id: int) -> list[KeyRow]:
        rows = await self._db.fetchall(
            """
            SELECT k.*, p.name AS pool_name
              FROM key_pool_keys k
              JOIN key_pools p ON p.id = k.pool_id
             WHERE k.guild_id = ? AND k.pool_id = ?
             ORDER BY k.id
            """,
            (guild_id, pool_id),
        )
        return [_to_key(row) for row in rows]

    async def available_count(self, guild_id: int, pool_ids: Sequence[int]) -> int:
        if not pool_ids:
            return 0
        placeholders = ", ".join("?" for _ in pool_ids)
        row = await self._db.fetchone(
            f"""
            SELECT COUNT(*) AS total
              FROM key_pool_keys
             WHERE guild_id = ? AND status = 'available'
               AND pool_id IN ({placeholders})
            """,
            (guild_id, *pool_ids),
        )
        return int(row["total"]) if row is not None else 0

    async def void_available(self, guild_id: int, pool_id: int) -> int:
        return await self._db.execute(
            """
            UPDATE key_pool_keys
               SET status = 'void', voided_at = ?
             WHERE guild_id = ? AND pool_id = ? AND status = 'available'
            """,
            (utcnow_iso(), guild_id, pool_id),
        )

    # ------------------------------------------------------------------ 活动

    async def create_drop(
        self,
        guild_id: int,
        *,
        pool_ids: Sequence[int],
        mode: str,
        count_mode: str,
        key_count: int | None,
        role_id: int | None,
        deny_role_id: int | None,
        channel_id: int,
        closes_at: str | None,
        created_by: int,
    ) -> KeyDrop:
        if mode not in MODES:
            raise DropError(f"未知模式：{mode}")
        if count_mode not in COUNT_MODES:
            raise DropError(f"未知数量模式：{count_mode}")
        if count_mode == COUNT_FIXED and (key_count is None or key_count < 1):
            raise DropError("固定数量必须 >= 1")
        if count_mode == COUNT_FOLLOW and key_count is not None:
            raise DropError("跟随库存模式不能指定固定数量")
        if not pool_ids:
            raise DropError("至少选择一个池")

        pools = [await self.get_pool(guild_id, pool_id) for pool_id in pool_ids]
        if any(pool is None for pool in pools):
            raise DropError("池不存在")
        if any(pool is not None and not pool.enabled for pool in pools):
            raise DropError("池已停用")

        last_error: sqlite3.IntegrityError | None = None
        for _ in range(MAX_RESERVE_ATTEMPTS * 2):
            code = _new_code()
            try:
                async with self._db.transaction() as connection:
                    cursor = await connection.execute(
                        """
                        INSERT INTO key_pool_drops
                            (guild_id, code, mode, count_mode, key_count, role_id,
                             deny_role_id, channel_id, closes_at, status, delivered,
                             created_by, created_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'open', 0, ?, ?)
                        """,
                        (
                            guild_id,
                            code,
                            mode,
                            count_mode,
                            key_count,
                            role_id,
                            deny_role_id,
                            channel_id,
                            closes_at,
                            created_by,
                            utcnow_iso(),
                        ),
                    )
                    drop_id = int(cursor.lastrowid or 0)
                    await connection.executemany(
                        "INSERT INTO key_pool_drop_pools (drop_id, pool_id) VALUES (?, ?)",
                        [(drop_id, pool_id) for pool_id in dict.fromkeys(pool_ids)],
                    )
                drop = await self.get_drop(guild_id, code)
                assert drop is not None
                return drop
            except sqlite3.IntegrityError as exc:
                last_error = exc
                continue
        raise DropError("生成活动短码失败") from last_error

    async def get_drop(self, guild_id: int, code: str) -> KeyDrop | None:
        row = await self._db.fetchone(
            "SELECT * FROM key_pool_drops WHERE guild_id = ? AND code = ?",
            (guild_id, code),
        )
        return _to_drop(row) if row is not None else None

    async def get_drop_by_id(self, guild_id: int, drop_id: int) -> KeyDrop | None:
        row = await self._db.fetchone(
            "SELECT * FROM key_pool_drops WHERE guild_id = ? AND id = ?",
            (guild_id, drop_id),
        )
        return _to_drop(row) if row is not None else None

    async def drop_by_card(self, message_id: int) -> KeyDrop | None:
        row = await self._db.fetchone(
            """
            SELECT d.*
              FROM key_pool_drop_cards c
              JOIN key_pool_drops d ON d.id = c.drop_id
             WHERE c.message_id = ?
             ORDER BY c.id DESC
             LIMIT 1
            """,
            (message_id,),
        )
        return _to_drop(row) if row is not None else None

    async def due_drops(self, now_iso: str) -> list[KeyDrop]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM key_pool_drops
             WHERE status = 'open' AND closes_at IS NOT NULL AND closes_at <= ?
             ORDER BY id
            """,
            (now_iso,),
        )
        return [_to_drop(row) for row in rows]

    async def list_drops(
        self,
        guild_id: int,
        *,
        statuses: Sequence[str] | None = None,
        limit: int = 25,
    ) -> list[KeyDrop]:
        sql = "SELECT * FROM key_pool_drops WHERE guild_id = ?"
        params: list[Any] = [guild_id]
        if statuses:
            placeholders = ", ".join("?" for _ in statuses)
            sql += f" AND status IN ({placeholders})"
            params.extend(statuses)
        sql += " ORDER BY id DESC LIMIT ?"
        params.append(limit)
        rows = await self._db.fetchall(sql, params)
        return [_to_drop(row) for row in rows]

    async def drop_pools(self, drop_id: int) -> list[KeyPool]:
        rows = await self._db.fetchall(
            """
            SELECT p.*
              FROM key_pool_drop_pools dp
              JOIN key_pools p ON p.id = dp.pool_id
             WHERE dp.drop_id = ?
             ORDER BY p.name
            """,
            (drop_id,),
        )
        return [_to_pool(row) for row in rows]

    async def add_pool_to_drop(self, guild_id: int, drop_id: int, pool_id: int) -> None:
        drop = await self.get_drop_by_id(guild_id, drop_id)
        if drop is None or drop.status == DROP_CLOSED:
            raise DropError("活动不存在或已结束")
        pool = await self.get_pool(guild_id, pool_id)
        if pool is None or not pool.enabled:
            raise DropError("池不存在或已停用")
        await self._db.execute(
            "INSERT OR IGNORE INTO key_pool_drop_pools (drop_id, pool_id) VALUES (?, ?)",
            (drop_id, pool_id),
        )

    async def remove_pool_from_drop(self, guild_id: int, drop_id: int, pool_id: int) -> None:
        drop = await self.get_drop_by_id(guild_id, drop_id)
        if drop is None or drop.status == DROP_CLOSED:
            raise DropError("活动不存在或已结束")
        pools = await self.drop_pools(drop_id)
        if len(pools) <= 1:
            raise DropError("活动必须至少保留一个池")
        await self._db.execute(
            "DELETE FROM key_pool_drop_pools WHERE drop_id = ? AND pool_id = ?",
            (drop_id, pool_id),
        )

    async def close_drop(self, guild_id: int, drop_id: int, *, status: str = DROP_CLOSED) -> bool:
        if status not in (DROP_SETTLING, DROP_CLOSED):
            raise DropError(f"未知状态：{status}")
        changed = await self._db.execute(
            """
            UPDATE key_pool_drops
               SET status = ?
             WHERE guild_id = ? AND id = ? AND status != 'closed'
            """,
            (status, guild_id, drop_id),
        )
        return bool(changed)

    async def set_retry_until(self, guild_id: int, drop_id: int, retry_until: str | None) -> None:
        await self._db.execute(
            "UPDATE key_pool_drops SET retry_until = ? WHERE guild_id = ? AND id = ?",
            (retry_until, guild_id, drop_id),
        )

    async def due_settling(self, now_iso: str) -> list[KeyDrop]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM key_pool_drops
             WHERE status = 'settling' AND retry_until IS NOT NULL AND retry_until <= ?
             ORDER BY id
            """,
            (now_iso,),
        )
        return [_to_drop(row) for row in rows]

    async def active_drops_for_pool(self, guild_id: int, pool_id: int) -> list[KeyDrop]:
        rows = await self._db.fetchall(
            """
            SELECT d.*
              FROM key_pool_drops d
              JOIN key_pool_drop_pools dp ON dp.drop_id = d.id
             WHERE d.guild_id = ? AND dp.pool_id = ?
               AND d.status IN ('open', 'settling')
             ORDER BY d.id
            """,
            (guild_id, pool_id),
        )
        return [_to_drop(row) for row in rows]

    async def sole_dependent_drops(self, guild_id: int, pool_id: int) -> list[KeyDrop]:
        """引用该池、且只引用了这一个池的未结束活动。"""
        drops = await self.active_drops_for_pool(guild_id, pool_id)
        blocking: list[KeyDrop] = []
        for drop in drops:
            pools = await self.drop_pools(drop.id)
            if len(pools) == 1:
                blocking.append(drop)
        return blocking

    async def stop_pool(self, guild_id: int, pool_id: int) -> list[str]:
        """停用池；若存在唯一依赖则拒绝。成功时从有替代池的活动里移除该池。"""
        if await self.get_pool(guild_id, pool_id) is None:
            raise KeyPoolError("池不存在")
        blocking = await self.sole_dependent_drops(guild_id, pool_id)
        if blocking:
            raise PoolInUse([drop.code for drop in blocking])
        deps = await self.active_drops_for_pool(guild_id, pool_id)
        await self._db.execute(
            "UPDATE key_pools SET enabled = 0 WHERE guild_id = ? AND id = ?",
            (guild_id, pool_id),
        )
        for drop in deps:
            await self._db.execute(
                "DELETE FROM key_pool_drop_pools WHERE drop_id = ? AND pool_id = ?",
                (drop.id, pool_id),
            )
        return [drop.code for drop in deps]

    # ------------------------------------------------------------------ 入口卡片

    async def add_card(
        self,
        drop_id: int,
        *,
        channel_id: int,
        message_id: int | None,
        created_by: int,
        status: str = CARD_ACTIVE,
    ) -> DropCard:
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                """
                INSERT INTO key_pool_drop_cards
                    (drop_id, channel_id, message_id, status, created_by, created_at)
                VALUES (?, ?, ?, ?, ?, ?)
                """,
                (drop_id, channel_id, message_id, status, created_by, utcnow_iso()),
            )
            card_id = int(cursor.lastrowid or 0)
        row = await self._db.fetchone("SELECT * FROM key_pool_drop_cards WHERE id = ?", (card_id,))
        assert row is not None
        return _to_card(row)

    async def cards(self, drop_id: int) -> list[DropCard]:
        rows = await self._db.fetchall(
            "SELECT * FROM key_pool_drop_cards WHERE drop_id = ? ORDER BY id",
            (drop_id,),
        )
        return [_to_card(row) for row in rows]

    async def set_card_status(self, card_id: int, status: str) -> bool:
        changed = await self._db.execute(
            "UPDATE key_pool_drop_cards SET status = ? WHERE id = ?",
            (status, card_id),
        )
        return bool(changed)

    async def deactivate_cards_in_channel(self, drop_id: int, channel_id: int) -> list[DropCard]:
        rows = await self._db.fetchall(
            """
            SELECT * FROM key_pool_drop_cards
             WHERE drop_id = ? AND channel_id = ? AND status = 'active'
             ORDER BY id
            """,
            (drop_id, channel_id),
        )
        cards = [_to_card(row) for row in rows]
        for card in cards:
            await self.set_card_status(card.id, CARD_CLOSED)
        return cards

    # ------------------------------------------------------------------ 报名

    async def add_entry(self, drop_id: int, user_id: int) -> bool:
        cursor = await self._db.execute(
            """
            INSERT OR IGNORE INTO key_pool_drop_entries (drop_id, user_id, entered_at)
            VALUES (?, ?, ?)
            """,
            (drop_id, user_id, utcnow_iso()),
        )
        return bool(cursor)

    async def entry_count(self, drop_id: int) -> int:
        row = await self._db.fetchone(
            "SELECT COUNT(*) AS total FROM key_pool_drop_entries WHERE drop_id = ?",
            (drop_id,),
        )
        return int(row["total"]) if row is not None else 0

    async def has_delivery(self, drop_id: int, user_id: int) -> bool:
        row = await self._db.fetchone(
            """
            SELECT 1 FROM key_pool_keys
             WHERE drop_id = ? AND delivered_to = ? AND status IN ('reserved', 'delivered')
             LIMIT 1
            """,
            (drop_id, user_id),
        )
        return row is not None

    async def entries(self, drop_id: int) -> list[tuple[int, str]]:
        rows = await self._db.fetchall(
            "SELECT user_id, entered_at FROM key_pool_drop_entries WHERE drop_id = ? ORDER BY entered_at",
            (drop_id,),
        )
        return [(int(row["user_id"]), str(row["entered_at"])) for row in rows]

    # ------------------------------------------------------------------ 未送达

    async def record_failure(self, drop_id: int, user_id: int, reason: str) -> None:
        now = utcnow_iso()
        await self._db.execute(
            """
            INSERT INTO key_pool_drop_failures (drop_id, user_id, reason, created_at, retried_at)
            VALUES (?, ?, ?, ?, NULL)
            ON CONFLICT (drop_id, user_id) DO UPDATE SET
                reason = excluded.reason,
                created_at = excluded.created_at,
                retried_at = NULL
            """,
            (drop_id, user_id, reason, now),
        )

    async def clear_failure(self, drop_id: int, user_id: int) -> None:
        await self._db.execute(
            "DELETE FROM key_pool_drop_failures WHERE drop_id = ? AND user_id = ?",
            (drop_id, user_id),
        )

    async def failures(self, drop_id: int) -> list[tuple[int, str, str]]:
        rows = await self._db.fetchall(
            "SELECT user_id, reason, created_at FROM key_pool_drop_failures WHERE drop_id = ? ORDER BY created_at",
            (drop_id,),
        )
        return [(int(row["user_id"]), str(row["reason"]), str(row["created_at"])) for row in rows]

    # ------------------------------------------------------------------ 领取

    async def reserve(
        self,
        guild_id: int,
        pool_ids: Sequence[int],
        *,
        user_id: int,
        drop_id: int | None = None,
    ) -> Reservation | None:
        """从所选池里随机取一枚可用 key 并锁定。没有库存返回 ``None``。"""
        if not pool_ids:
            return None
        placeholders = ", ".join("?" for _ in pool_ids)
        async with self._db.transaction() as connection:
            cursor = await connection.execute(
                f"""
                UPDATE key_pool_keys
                   SET status = 'reserved',
                       reserved_at = ?,
                       delivered_to = ?,
                       drop_id = ?
                 WHERE id = (
                       SELECT id
                         FROM key_pool_keys
                        WHERE guild_id = ? AND status = 'available'
                          AND pool_id IN ({placeholders})
                        ORDER BY RANDOM()
                        LIMIT 1
                 )
                RETURNING id, guild_id, pool_id, secret
                """,
                (utcnow_iso(), user_id, drop_id, guild_id, *pool_ids),
            )
            row = await cursor.fetchone()
        if row is None:
            return None
        return Reservation(
            key_id=int(row["id"]),
            guild_id=int(row["guild_id"]),
            pool_id=int(row["pool_id"]),
            secret=str(row["secret"]),
            drop_id=drop_id,
            user_id=user_id,
        )

    async def commit(self, reservation: Reservation) -> bool:
        changed = await self._db.execute(
            """
            UPDATE key_pool_keys
               SET status = 'delivered',
                   delivered_at = ?,
                   reserved_at = NULL
             WHERE id = ? AND status = 'reserved'
            """,
            (utcnow_iso(), reservation.key_id),
        )
        if changed and reservation.drop_id is not None:
            await self._db.execute(
                "UPDATE key_pool_drops SET delivered = delivered + 1 WHERE id = ?",
                (reservation.drop_id,),
            )
        return bool(changed)

    async def release(self, reservation: Reservation) -> bool:
        changed = await self._db.execute(
            """
            UPDATE key_pool_keys
               SET status = 'available',
                   reserved_at = NULL,
                   delivered_to = NULL,
                   drop_id = NULL
             WHERE id = ? AND status = 'reserved'
            """,
            (reservation.key_id,),
        )
        return bool(changed)

    async def sweep_reserved(self, *, cutoff_iso: str) -> int:
        """把超时的 reserved 收回 available；返回收回数量。"""
        return await self._db.execute(
            """
            UPDATE key_pool_keys
               SET status = 'available',
                   reserved_at = NULL,
                   delivered_to = NULL,
                   drop_id = NULL
             WHERE status = 'reserved' AND reserved_at IS NOT NULL AND reserved_at < ?
            """,
            (cutoff_iso,),
        )


def _new_code() -> str:
    return "".join(secrets.choice(CODE_ALPHABET) for _ in range(CODE_LENGTH))


def _to_pool(row: Any) -> KeyPool:
    return KeyPool(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        name=str(row["name"]),
        note=row["note"],
        enabled=bool(row["enabled"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )


def _to_key(row: Any) -> KeyRow:
    return KeyRow(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        pool_id=int(row["pool_id"]),
        pool_name=str(row["pool_name"]),
        secret=str(row["secret"]),
        status=str(row["status"]),
        added_at=str(row["added_at"]),
        delivered_at=row["delivered_at"],
        delivered_to=(int(row["delivered_to"]) if row["delivered_to"] is not None else None),
        drop_id=(int(row["drop_id"]) if row["drop_id"] is not None else None),
        voided_at=row["voided_at"],
    )


def _to_drop(row: Any) -> KeyDrop:
    return KeyDrop(
        id=int(row["id"]),
        guild_id=int(row["guild_id"]),
        code=str(row["code"]),
        mode=str(row["mode"]),
        count_mode=str(row["count_mode"]),
        key_count=(int(row["key_count"]) if row["key_count"] is not None else None),
        role_id=(int(row["role_id"]) if row["role_id"] is not None else None),
        deny_role_id=(int(row["deny_role_id"]) if row["deny_role_id"] is not None else None),
        channel_id=int(row["channel_id"]),
        closes_at=row["closes_at"],
        status=str(row["status"]),
        delivered=int(row["delivered"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
        retry_until=row["retry_until"],
    )


def _to_card(row: Any) -> DropCard:
    return DropCard(
        id=int(row["id"]),
        drop_id=int(row["drop_id"]),
        channel_id=int(row["channel_id"]),
        message_id=(int(row["message_id"]) if row["message_id"] is not None else None),
        status=str(row["status"]),
        created_by=int(row["created_by"]),
        created_at=str(row["created_at"]),
    )
