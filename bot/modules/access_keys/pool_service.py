"""`access_keys` 对其它模块暴露的 key 服务。

`forms` 只依赖这里的接口，不 import access_keys 的内部实现；access_keys 被停用/重载时，
服务从 ``bot.services`` 注销，forms 下次调用就会拿到 ``None`` 并暂停 key 联动。
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from dataclasses import dataclass

from bot.integrations.keypool import (
    DROP_OPEN,
    DROP_SETTLING,
    KeyDrop,
    KeyPool,
    KeyPoolStore,
    PoolStats,
    Reservation,
)

SERVICE_NAME = "keys"


@dataclass(frozen=True)
class PoolSummary:
    pool: KeyPool
    stats: PoolStats


class KeyPoolService:
    """面板层与 forms 共用的 key 能力。"""

    def __init__(self, store: KeyPoolStore, *, logger: logging.Logger | None = None) -> None:
        self.store = store
        self._logger = logger or logging.getLogger("bot.access_keys")

    async def summaries(self, guild_id: int, *, include_disabled: bool = True) -> list[PoolSummary]:
        pools = await self.store.list_pools(guild_id, include_disabled=include_disabled)
        return [PoolSummary(pool=pool, stats=await self.store.pool_stats(guild_id, pool.id)) for pool in pools]

    async def available(self, guild_id: int, pool_ids: Sequence[int]) -> int:
        return await self.store.available_count(guild_id, pool_ids)

    async def active_drops(self, guild_id: int) -> list[KeyDrop]:
        return await self.store.list_drops(guild_id, statuses=(DROP_OPEN, DROP_SETTLING))

    async def drop(self, guild_id: int, drop_id: int) -> KeyDrop | None:
        return await self.store.get_drop_by_id(guild_id, drop_id)

    async def drop_by_code(self, guild_id: int, code: str) -> KeyDrop | None:
        return await self.store.get_drop(guild_id, code)

    async def drop_pools(self, drop_id: int) -> list[KeyPool]:
        return await self.store.drop_pools(drop_id)

    async def available_for_drop(self, guild_id: int, drop_id: int) -> int:
        pools = await self.store.drop_pools(drop_id)
        return await self.store.available_count(guild_id, [pool.id for pool in pools])

    async def reserve(
        self,
        guild_id: int,
        pool_ids: Sequence[int],
        *,
        user_id: int,
        drop_id: int | None = None,
    ) -> Reservation | None:
        return await self.store.reserve(guild_id, pool_ids, user_id=user_id, drop_id=drop_id)

    async def commit(self, reservation: Reservation) -> bool:
        return await self.store.commit(reservation)

    async def release(self, reservation: Reservation) -> bool:
        return await self.store.release(reservation)

    async def has_delivery(self, drop_id: int, user_id: int) -> bool:
        return await self.store.has_delivery(drop_id, user_id)
