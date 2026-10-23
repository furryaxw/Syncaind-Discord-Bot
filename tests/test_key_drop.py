"""`/key` 活动：创建、先到先得领取、抽奖开奖与 DM 失败退回。"""

from __future__ import annotations

from bot.integrations.keypool import (
    COUNT_FIXED,
    COUNT_FOLLOW,
    DROP_CLOSED,
    MODE_FCFS,
    MODE_RAFFLE,
    KeyPoolStore,
)
from bot.modules.access_keys.cog import AccessKeysCog
from tests.conftest import GUILD_ID
from tests.discord_fakes import (
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    FakeMessage,
    build_guild,
    setup_bot,
)


async def prepare(settings):
    bot = await setup_bot(settings)
    guild = build_guild()
    interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
    store = KeyPoolStore(bot.db)
    pool = await store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
    await store.add_keys(GUILD_ID, pool.id, ("K1", "K2", "K3"))
    cog = bot.cogs["AccessKeysCog"]
    assert isinstance(cog, AccessKeysCog)
    return bot, guild, interaction, cog, pool.id


async def create_drop(cog, interaction, pool_id: int, *, mode: str, count_mode: str, key_count=None):
    await cog._create_drop(
        interaction,
        pool_ids=[pool_id],
        mode=mode,
        count_mode=count_mode,
        key_count=key_count,
        role_id=None,
        deny_role_id=None,
        minutes=10,
    )
    drops = await cog.store.list_drops(GUILD_ID)
    assert drops
    return drops[0]


async def test_create_fcfs_drop_posts_a_card(settings) -> None:
    bot, _guild, interaction, cog, pool_id = await prepare(settings)
    try:
        drop = await create_drop(
            cog, interaction, pool_id, mode=MODE_FCFS, count_mode=COUNT_FOLLOW
        )
        cards = await cog.store.cards(drop.id)
    finally:
        await bot.db.close()

    assert drop.code
    assert len(cards) == 1
    assert cards[0].message_id is not None
    assert interaction.response._messages[-1]["embed"].title == "活动已创建"


async def test_fcfs_claim_sends_a_key_by_dm(settings) -> None:
    bot, guild, interaction, cog, pool_id = await prepare(settings)
    try:
        drop = await create_drop(
            cog, interaction, pool_id, mode=MODE_FCFS, count_mode=COUNT_FOLLOW
        )
        card = (await cog.store.cards(drop.id))[0]
        member = guild.get_member(MODERATOR_ID)
        assert member is not None
        claim = FakeInteraction(user=member, guild=guild)
        claim.message = FakeMessage(card.message_id or 0, interaction.channel)

        await cog._handle_fcfs(claim)
        stats = await cog.store.pool_stats(GUILD_ID, pool_id)
    finally:
        await bot.db.close()

    assert member._dms and member._dms[-1].startswith("你的激活码")
    assert stats.available == 2
    assert stats.delivered == 1
    assert claim.response._messages[-1]["embed"].title == "已领取"


async def test_fcfs_claim_returns_the_key_when_dm_fails(settings) -> None:
    bot, guild, interaction, cog, pool_id = await prepare(settings)
    try:
        drop = await create_drop(
            cog, interaction, pool_id, mode=MODE_FCFS, count_mode=COUNT_FOLLOW
        )
        card = (await cog.store.cards(drop.id))[0]
        member = guild.get_member(TARGET_ID)
        assert member is not None
        member._fail_dm = True
        claim = FakeInteraction(user=member, guild=guild)
        claim.message = FakeMessage(card.message_id or 0, interaction.channel)

        await cog._handle_fcfs(claim)
        stats = await cog.store.pool_stats(GUILD_ID, pool_id)
    finally:
        await bot.db.close()

    assert stats.available == 3
    assert stats.delivered == 0
    assert "key 已退回" in claim.response._messages[-1]["embed"].title


async def test_raffle_draw_delivers_one_winners_key(settings) -> None:
    bot, guild, interaction, cog, pool_id = await prepare(settings)
    try:
        drop = await cog.store.create_drop(
            GUILD_ID,
            pool_ids=[pool_id],
            mode=MODE_RAFFLE,
            count_mode=COUNT_FIXED,
            key_count=1,
            role_id=None,
            deny_role_id=None,
            channel_id=interaction.channel_id or 0,
            closes_at=None,
            created_by=MODERATOR_ID,
        )
        await cog.store.add_entry(drop.id, TARGET_ID)
        await cog.store.add_entry(drop.id, MODERATOR_ID)

        delivered, failures = await cog._draw_drop(guild, drop)
        updated = await cog.store.get_drop(GUILD_ID, drop.code)
        stats = await cog.store.pool_stats(GUILD_ID, pool_id)
    finally:
        await bot.db.close()

    assert len(delivered) + len(failures) == 1
    assert updated is not None
    assert updated.status == DROP_CLOSED
    assert stats.delivered == 1


async def test_drop_can_start_without_a_public_entry(settings) -> None:
    bot, _guild, interaction, cog, pool_id = await prepare(settings)
    try:
        await cog._create_drop(
            interaction,
            pool_ids=[pool_id],
            mode=MODE_FCFS,
            count_mode=COUNT_FOLLOW,
            key_count=None,
            role_id=None,
            deny_role_id=None,
            minutes=10,
            auto_entry=False,
        )
        drop = (await cog.store.list_drops(GUILD_ID))[0]
        assert await cog.store.cards(drop.id) == []

        await cog._add_entry(interaction, drop)
        await cog._add_entry(interaction, drop)
        cards = await cog.store.cards(drop.id)
    finally:
        await bot.db.close()

    assert len(cards) == 2
    assert sum(1 for card in cards if card.status == "active") == 1
    assert "入口已添加" in interaction.response._messages[-1]["embed"].title


async def test_fixed_fcfs_closes_after_the_last_key(settings) -> None:
    bot, guild, interaction, cog, pool_id = await prepare(settings)
    try:
        drop = await create_drop(
            cog, interaction, pool_id, mode=MODE_FCFS, count_mode=COUNT_FIXED, key_count=1
        )
        card = (await cog.store.cards(drop.id))[0]
        first = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        first.message = FakeMessage(card.message_id or 0, interaction.channel)
        await cog._handle_fcfs(first)

        second = FakeInteraction(user=guild.get_member(TARGET_ID), guild=guild)
        second.message = FakeMessage(card.message_id or 0, interaction.channel)
        await cog._handle_fcfs(second)
        updated = await cog.store.get_drop(GUILD_ID, drop.code)
    finally:
        await bot.db.close()

    assert updated is not None
    assert updated.status == DROP_CLOSED
    assert "已经结束" in second.response._messages[-1]["embed"].title


async def test_fcfs_close_marks_the_drop_closed_without_a_draw(settings) -> None:
    bot, _guild, interaction, cog, pool_id = await prepare(settings)
    try:
        drop = await create_drop(
            cog, interaction, pool_id, mode=MODE_FCFS, count_mode=COUNT_FOLLOW
        )
        await cog._close_drop(interaction, drop)
        updated = await cog.store.get_drop(GUILD_ID, drop.code)
    finally:
        await bot.db.close()

    assert updated is not None
    assert updated.status == DROP_CLOSED
