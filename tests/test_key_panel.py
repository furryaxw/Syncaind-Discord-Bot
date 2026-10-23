"""`/key` 面板（阶段一）：主面板、池列表、导出与导入落库。"""

from __future__ import annotations

from bot.integrations.keypool import KeyPoolStore
from bot.modules.access_keys.cog import AccessKeysCog
from bot.modules.access_keys.importing import ParsedKeys
from tests.conftest import GUILD_ID
from tests.discord_fakes import (
    MODERATOR_ID,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)


async def make_bot(settings):
    return await setup_bot(settings)


async def test_key_command_shows_pool_totals(settings) -> None:
    bot = await make_bot(settings)
    try:
        store = KeyPoolStore(bot.db)
        pool = await store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
        await store.add_keys(GUILD_ID, pool.id, ("K1", "K2"))

        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        await run_command(bot, "key", interaction)
    finally:
        await bot.db.close()

    embed = interaction.response._messages[-1]["embed"]
    assert embed.title == "激活码池"
    assert "池 1 个" in embed.description
    assert "可用 2 枚" in embed.description


async def test_key_module_registers_the_keys_service(settings) -> None:
    bot = await make_bot(settings)
    try:
        assert bot.services.get("keys") is not None
        assert "AccessKeysCog" in bot.cogs
    finally:
        await bot.db.close()


async def test_pool_list_and_detail_are_rendered(settings) -> None:
    bot = await make_bot(settings)
    try:
        store = KeyPoolStore(bot.db)
        pool = await store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
        await store.add_keys(GUILD_ID, pool.id, ("K1",))
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        cog = bot.cogs["AccessKeysCog"]
        assert isinstance(cog, AccessKeysCog)

        await cog._show_main(interaction, edit=False)
        await cog._show_pools(interaction)
        await cog._show_pool_detail(interaction, pool.id)
    finally:
        await bot.db.close()

    detail = interaction.response._edits[-1]["embed"]
    assert detail.title == "池：A"
    assert "可用 1" in detail.description


async def test_import_into_existing_pool(settings) -> None:
    bot = await make_bot(settings)
    try:
        store = KeyPoolStore(bot.db)
        pool = await store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        cog = bot.cogs["AccessKeysCog"]
        assert isinstance(cog, AccessKeysCog)

        await cog._import_into(
            interaction,
            ParsedKeys(keys=("K1", "K2"), invalid=(), duplicates_in_file=0),
            pool.id,
        )
        stats = await store.pool_stats(GUILD_ID, pool.id)
    finally:
        await bot.db.close()

    assert stats.available == 2
    assert interaction.response._messages[-1]["embed"].title == "导入完成"


async def test_export_pool_sends_csv(settings) -> None:
    bot = await make_bot(settings)
    try:
        store = KeyPoolStore(bot.db)
        pool = await store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
        await store.add_keys(GUILD_ID, pool.id, ("K1",))
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        cog = bot.cogs["AccessKeysCog"]
        assert isinstance(cog, AccessKeysCog)

        await cog._export_pool(interaction, pool.id)
    finally:
        await bot.db.close()

    sent = interaction.response._messages[-1]
    assert sent["file"].filename == f"pool-{pool.id}.csv"


async def test_pool_toggle_stops_a_pool(settings) -> None:
    bot = await make_bot(settings)
    try:
        store = KeyPoolStore(bot.db)
        pool = await store.create_pool(GUILD_ID, name="A", note=None, created_by=MODERATOR_ID)
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        cog = bot.cogs["AccessKeysCog"]
        assert isinstance(cog, AccessKeysCog)

        await cog._show_pool_detail(interaction, pool.id)
        await store.stop_pool(GUILD_ID, pool.id)
        await cog._show_pool_detail(interaction, pool.id)
        stopped = await store.get_pool(GUILD_ID, pool.id)
    finally:
        await bot.db.close()

    assert stopped is not None
    assert stopped.enabled is False
