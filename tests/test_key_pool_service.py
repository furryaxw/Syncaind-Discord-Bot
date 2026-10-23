"""key 服务层：面板与 forms 共用的池摘要、库存与预留。"""

from __future__ import annotations

import pytest

from bot.integrations.keypool import KeyPoolStore
from bot.modules.access_keys.pool_service import KeyPoolService

GUILD_ID = 100
USER_ID = 200


@pytest.fixture
def service(db) -> KeyPoolService:
    return KeyPoolService(KeyPoolStore(db))


async def test_summaries_include_stats(service: KeyPoolService) -> None:
    pool = await service.store.create_pool(GUILD_ID, name="A", note=None, created_by=USER_ID)
    await service.store.add_keys(GUILD_ID, pool.id, ("K1", "K2"))

    summaries = await service.summaries(GUILD_ID)

    assert len(summaries) == 1
    assert summaries[0].pool.name == "A"
    assert summaries[0].stats.available == 2


async def test_available_and_reserve_commit(service: KeyPoolService) -> None:
    pool = await service.store.create_pool(GUILD_ID, name="A", note=None, created_by=USER_ID)
    await service.store.add_keys(GUILD_ID, pool.id, ("K1", "K2"))

    assert await service.available(GUILD_ID, [pool.id]) == 2

    reservation = await service.reserve(GUILD_ID, [pool.id], user_id=USER_ID)
    assert reservation is not None

    assert await service.commit(reservation) is True
