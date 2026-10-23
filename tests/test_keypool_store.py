"""本地 key 池的存储层：导入、库存、预留、活动与唯一依赖。"""

from __future__ import annotations

import pytest

from bot.integrations.keypool import (
    COUNT_FIXED,
    COUNT_FOLLOW,
    MODE_FCFS,
    MODE_RAFFLE,
    DropError,
    KeyPoolStore,
    PoolInUse,
    PoolNameTaken,
    PoolNotEmpty,
)

GUILD_ID = 100
USER_ID = 200


@pytest.fixture
def store(db) -> KeyPoolStore:
    return KeyPoolStore(db)


async def make_pool(store: KeyPoolStore, name: str = "默认池", keys: tuple[str, ...] = ()) -> int:
    pool = await store.create_pool(GUILD_ID, name=name, note=None, created_by=USER_ID)
    if keys:
        await store.add_keys(GUILD_ID, pool.id, keys)
    return pool.id


async def test_create_pool_rejects_duplicate_name(store: KeyPoolStore) -> None:
    await store.create_pool(GUILD_ID, name="A", note=None, created_by=USER_ID)

    with pytest.raises(PoolNameTaken):
        await store.create_pool(GUILD_ID, name="A", note=None, created_by=USER_ID)


async def test_add_keys_dedupes_within_input_and_against_existing(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store)

    inserted, skipped = await store.add_keys(GUILD_ID, pool_id, ["K1", "K2", "K1", " ", "K3"])

    assert (inserted, skipped) == (3, 1)

    inserted, skipped = await store.add_keys(GUILD_ID, pool_id, ["K2", "K4"])

    assert (inserted, skipped) == (1, 1)


async def test_pool_stats_and_available_count(store: KeyPoolStore) -> None:
    pool_a = await make_pool(store, "A", ("K1", "K2", "K3"))
    pool_b = await make_pool(store, "B", ("K4", "K5"))

    stats = await store.pool_stats(GUILD_ID, pool_a)

    assert stats.available == 3
    assert stats.total == 3
    assert await store.available_count(GUILD_ID, [pool_a, pool_b]) == 5


async def test_reserve_commit_and_release(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1", "K2", "K3"))

    reservation = await store.reserve(GUILD_ID, [pool_id], user_id=USER_ID)

    assert reservation is not None
    assert reservation.secret in {"K1", "K2", "K3"}
    assert await store.available_count(GUILD_ID, [pool_id]) == 2

    assert await store.release(reservation) is True
    assert await store.available_count(GUILD_ID, [pool_id]) == 3

    reservation = await store.reserve(GUILD_ID, [pool_id], user_id=USER_ID)
    assert reservation is not None
    assert await store.commit(reservation) is True

    stats = await store.pool_stats(GUILD_ID, pool_id)
    assert stats.available == 2
    assert stats.delivered == 1


async def test_reserve_returns_none_when_empty(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1",))

    first = await store.reserve(GUILD_ID, [pool_id], user_id=USER_ID)

    assert first is not None
    assert await store.reserve(GUILD_ID, [pool_id], user_id=USER_ID) is None


async def test_sweep_reserved_returns_timed_out_keys(store: KeyPoolStore, db) -> None:
    pool_id = await make_pool(store, "A", ("K1",))
    reservation = await store.reserve(GUILD_ID, [pool_id], user_id=USER_ID)
    assert reservation is not None
    await db.execute(
        "UPDATE key_pool_keys SET reserved_at = ? WHERE id = ?",
        ("2000-01-01T00:00:00+00:00", reservation.key_id),
    )

    swept = await store.sweep_reserved(cutoff_iso="2000-01-02T00:00:00+00:00")

    assert swept == 1
    assert await store.available_count(GUILD_ID, [pool_id]) == 1


async def test_drop_creation_and_pools(store: KeyPoolStore) -> None:
    pool_a = await make_pool(store, "A", ("K1",))
    pool_b = await make_pool(store, "B", ("K2",))

    drop = await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_a, pool_b],
        mode=MODE_RAFFLE,
        count_mode=COUNT_FIXED,
        key_count=2,
        role_id=None,
        deny_role_id=None,
        channel_id=999,
        closes_at=None,
        created_by=USER_ID,
    )

    assert len(drop.code) == 6
    assert drop.count_mode == COUNT_FIXED
    assert drop.key_count == 2
    assert {pool.id for pool in await store.drop_pools(drop.id)} == {pool_a, pool_b}


async def test_follow_mode_rejects_fixed_count(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1",))

    with pytest.raises(DropError):
        await store.create_drop(
            GUILD_ID,
            pool_ids=[pool_id],
            mode=MODE_FCFS,
            count_mode=COUNT_FOLLOW,
            key_count=1,
            role_id=None,
            deny_role_id=None,
            channel_id=1,
            closes_at=None,
            created_by=USER_ID,
        )


async def test_stop_pool_is_blocked_by_sole_dependency(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1",))
    await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_id],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=USER_ID,
    )

    with pytest.raises(PoolInUse):
        await store.stop_pool(GUILD_ID, pool_id)


async def test_stop_pool_removes_it_from_drops_with_alternatives(store: KeyPoolStore) -> None:
    pool_a = await make_pool(store, "A", ("K1",))
    pool_b = await make_pool(store, "B", ("K2",))
    drop = await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_a, pool_b],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=USER_ID,
    )

    removed = await store.stop_pool(GUILD_ID, pool_a)

    assert removed == [drop.code]
    assert [pool.id for pool in await store.drop_pools(drop.id)] == [pool_b]
    assert (await store.get_pool(GUILD_ID, pool_a)).enabled is False


async def test_stop_pool_is_blocked_if_any_drop_depends_only_on_it(store: KeyPoolStore) -> None:
    pool_a = await make_pool(store, "A", ("K1", "K2"))
    pool_b = await make_pool(store, "B", ("K3",))
    await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_a, pool_b],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=USER_ID,
    )
    await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_a],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=2,
        closes_at=None,
        created_by=USER_ID,
    )

    with pytest.raises(PoolInUse):
        await store.stop_pool(GUILD_ID, pool_a)


async def test_delete_pool_requires_empty_and_unreferenced(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1",))

    with pytest.raises(PoolNotEmpty):
        await store.delete_pool(GUILD_ID, pool_id)

    await store.void_available(GUILD_ID, pool_id)
    # 作废后仍算「有 key」，不能删。
    with pytest.raises(PoolNotEmpty):
        await store.delete_pool(GUILD_ID, pool_id)

    empty = await make_pool(store, "Empty")
    await store.delete_pool(GUILD_ID, empty)
    assert await store.get_pool(GUILD_ID, empty) is None


async def test_add_and_remove_pool_keeps_at_least_one(store: KeyPoolStore) -> None:
    pool_a = await make_pool(store, "A", ("K1",))
    pool_b = await make_pool(store, "B", ("K2",))
    drop = await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_a],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=USER_ID,
    )

    await store.add_pool_to_drop(GUILD_ID, drop.id, pool_b)
    assert len(await store.drop_pools(drop.id)) == 2

    await store.remove_pool_from_drop(GUILD_ID, drop.id, pool_b)
    with pytest.raises(DropError):
        await store.remove_pool_from_drop(GUILD_ID, drop.id, pool_a)


async def test_entries_are_idempotent(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1",))
    drop = await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_id],
        mode=MODE_RAFFLE,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=USER_ID,
    )

    assert await store.add_entry(drop.id, USER_ID) is True
    assert await store.add_entry(drop.id, USER_ID) is False
    assert await store.entry_count(drop.id) == 1


async def test_cards_can_be_added_and_listed(store: KeyPoolStore) -> None:
    pool_id = await make_pool(store, "A", ("K1",))
    drop = await store.create_drop(
        GUILD_ID,
        pool_ids=[pool_id],
        mode=MODE_FCFS,
        count_mode=COUNT_FOLLOW,
        key_count=None,
        role_id=None,
        deny_role_id=None,
        channel_id=1,
        closes_at=None,
        created_by=USER_ID,
    )

    card = await store.add_card(drop.id, channel_id=1, message_id=2, created_by=USER_ID)

    assert card.status == "active"
    assert [item.id for item in await store.cards(drop.id)] == [card.id]
