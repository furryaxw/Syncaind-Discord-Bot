"""`/github` 面板：自助绑定状态、按成员查询与反查。"""

from __future__ import annotations

from bot.integrations.github import GitHubAccountStore
from bot.modules.github_bridge.panel import GitHubPanelCog
from tests.conftest import GUILD_ID
from tests.discord_fakes import MODERATOR_ID, TARGET_ID, FakeInteraction, build_guild, run_command, setup_bot


async def prepare(settings):
    bot = await setup_bot(settings)
    guild = build_guild()
    interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
    store = GitHubAccountStore(bot.db)
    await store.link(
        GUILD_ID,
        discord_user_id=MODERATOR_ID,
        github_user_id=9001,
        github_login="Alice",
    )
    return bot, guild, interaction, store


async def test_panel_shows_own_binding(settings) -> None:
    bot, _guild, interaction, _store = await prepare(settings)
    try:
        await run_command(bot, "github", interaction)
    finally:
        await bot.db.close()

    embed = interaction.response._messages[-1]["embed"]
    assert embed.title == "GitHub 账号"
    assert "Alice" in embed.description
    assert "9001" in embed.description


async def test_get_member_shows_the_account(settings) -> None:
    bot, guild, interaction, _store = await prepare(settings)
    try:
        await run_command(bot, "github", interaction, get_member=guild.get_member(MODERATOR_ID))
    finally:
        await bot.db.close()

    embed = interaction.response._messages[-1]["embed"]
    assert "Alice" in embed.description


async def test_check_by_login_and_id(settings) -> None:
    bot, _guild, interaction, _store = await prepare(settings)
    cog = bot.cogs["GitHubPanelCog"]
    assert isinstance(cog, GitHubPanelCog)
    try:
        await cog._show_check(interaction, "alice")
        await cog._show_check(interaction, "9001")
    finally:
        await bot.db.close()

    assert "Alice" in interaction.response._messages[-1]["embed"].description
    assert "Alice" in interaction.response._messages[-2]["embed"].description


async def test_get_group_lists_bound_members(settings) -> None:
    bot, guild, interaction, _store = await prepare(settings)
    cog = bot.cogs["GitHubPanelCog"]
    assert isinstance(cog, GitHubPanelCog)
    role = guild.get_member(MODERATOR_ID).roles[0]
    try:
        await cog._show_group(interaction, role)
    finally:
        await bot.db.close()

    # 角色下的成员里只有 moderator 绑定了账号
    assert "Alice" in interaction.response._messages[-1]["embed"].description
    assert f"<@{TARGET_ID}>" not in interaction.response._messages[-1]["embed"].description
