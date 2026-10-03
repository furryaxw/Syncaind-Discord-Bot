"""GitHub ↔ Discord 映射表：绑定、覆盖、冲突、反查，以及 /link 命令的完整流程。"""

from __future__ import annotations

import asyncio
from typing import Any

import discord
import pytest

from bot.core.errors import UserError
from bot.integrations.github import GitHubAccountConflict, GitHubAccountStore
from bot.integrations.github import client as client_module
from tests.discord_fakes import (
    GUILD_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)
from tests.github_fakes import make_device_transport

GITHUB_ID = 42
GITHUB_LOGIN = "furryaxw"
OTHER_GITHUB_ID = 77
OTHER_GITHUB_LOGIN = "someoneelse"


def make_interaction(guild: Any, **kwargs: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild, **kwargs)


async def drain_background_tasks(cog: Any) -> None:
    """等后台轮询任务跑完——它就是命令真正启动的那个任务。"""
    for _ in range(50):
        tasks = list(getattr(cog, "_tasks", ()))
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("后台任务没有结束")


def last_edit(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._edits[-1]["embed"]


def last_message(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


# ---------------------------------------------------------------- 存储层


async def test_link_creates_a_row(db) -> None:
    store = GitHubAccountStore(db)

    account = await store.link(
        GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN
    )

    assert account.github_login == GITHUB_LOGIN
    assert account.linked_at != ""
    assert (await store.get(GUILD_ID, MODERATOR_ID)) == account


async def test_linking_again_replaces_the_previous_account(db) -> None:
    store = GitHubAccountStore(db)
    await store.link(GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    await store.link(
        GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=OTHER_GITHUB_ID, github_login=OTHER_GITHUB_LOGIN
    )

    assert [account.github_login for account in await store.all(GUILD_ID)] == [OTHER_GITHUB_LOGIN]


async def test_one_github_account_cannot_belong_to_two_people(db) -> None:
    """否则「按 GitHub 身份发角色」时根本不知道该发给谁。"""
    store = GitHubAccountStore(db)
    await store.link(GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    with pytest.raises(GitHubAccountConflict) as excinfo:
        await store.link(GUILD_ID, discord_user_id=TARGET_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    assert excinfo.value.holder_discord_user_id == MODERATOR_ID
    assert await store.get(GUILD_ID, TARGET_ID) is None


async def test_relinking_the_same_account_to_the_same_person_is_fine(db) -> None:
    store = GitHubAccountStore(db)
    await store.link(GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    account = await store.link(
        GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN
    )

    assert account.github_login == GITHUB_LOGIN


async def test_unlink_reports_whether_anything_was_removed(db) -> None:
    store = GitHubAccountStore(db)
    await store.link(GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    assert await store.unlink(GUILD_ID, MODERATOR_ID) == 1
    assert await store.unlink(GUILD_ID, MODERATOR_ID) == 0


async def test_lookup_by_login_is_case_insensitive(db) -> None:
    store = GitHubAccountStore(db)
    await store.link(GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    assert (await store.by_github_login(GUILD_ID, "FURRYAXW")) is not None
    assert (await store.by_github_login(GUILD_ID, "nobody")) is None


async def test_bindings_are_scoped_to_the_guild(db) -> None:
    store = GitHubAccountStore(db)
    await store.link(GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN)

    assert await store.get(GUILD_ID + 1, MODERATOR_ID) is None


# ---------------------------------------------------------------- /link github


async def test_link_without_configuration_explains_what_to_do(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "link github", interaction)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "github.not_configured"


async def test_link_shows_a_code_and_then_binds(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MIN_INTERVAL_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        bot.settings.github_oauth_client_id = "cid"
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = make_device_transport(
            polls=[{"access_token": "tok"}], user={"id": GITHUB_ID, "login": GITHUB_LOGIN}, interval=0
        )

        await run_command(bot, "link github", interaction)

        instructions = last_message(interaction).description
        assert "ABCD-1234" in instructions
        assert "https://github.com/login/device" in instructions

        await drain_background_tasks(cog)
        account = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert account is not None and account.github_login == GITHUB_LOGIN
    assert last_edit(interaction).title == "绑定成功"
    assert GITHUB_LOGIN in last_edit(interaction).description


async def test_link_waits_through_pending_polls(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MIN_INTERVAL_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        bot.settings.github_oauth_client_id = "cid"
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = make_device_transport(
            polls=[{"error": "authorization_pending"}, {"error": "slow_down"}, {"access_token": "tok"}],
            user={"id": GITHUB_ID, "login": GITHUB_LOGIN},
            interval=0,
        )

        await run_command(bot, "link github", interaction)
        await drain_background_tasks(cog)

        account = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert account is not None
    assert last_edit(interaction).title == "绑定成功"


async def test_link_reports_that_the_account_is_taken(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MIN_INTERVAL_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        bot.settings.github_oauth_client_id = "cid"
        await GitHubAccountStore(bot.db).link(
            GUILD_ID, discord_user_id=TARGET_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN
        )
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = make_device_transport(
            polls=[{"access_token": "tok"}], user={"id": GITHUB_ID, "login": GITHUB_LOGIN}, interval=0
        )

        await run_command(bot, "link github", interaction)
        await drain_background_tasks(cog)

        holder_bound = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert holder_bound is None
    assert last_edit(interaction).title == "绑定未完成"
    assert f"<@{TARGET_ID}>" in last_edit(interaction).description


async def test_link_reports_a_fatal_device_flow_error(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(client_module, "MIN_INTERVAL_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        bot.settings.github_oauth_client_id = "cid"
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = make_device_transport(polls=[{"error": "access_denied"}], interval=0)

        await run_command(bot, "link github", interaction)
        await drain_background_tasks(cog)
    finally:
        await bot.db.close()

    assert "access_denied" in last_edit(interaction).description


# ---------------------------------------------------------------- /link show


async def test_show_says_when_nothing_is_linked(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "link show", interaction)
    finally:
        await bot.db.close()

    assert last_message(interaction).title == "moderator 的 GitHub 绑定"
    assert "没有绑定" in last_message(interaction).description


async def test_show_prints_the_binding(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await GitHubAccountStore(bot.db).link(
            GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN
        )
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "link show", interaction)
    finally:
        await bot.db.close()

    description = last_message(interaction).description
    assert GITHUB_LOGIN in description
    assert str(GITHUB_ID) in description


async def test_looking_at_someone_else_requires_manage_roles(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild, permissions=discord.Permissions(send_messages=True))

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "link show", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.missing_permissions"


async def test_manage_roles_can_look_at_someone_else(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await GitHubAccountStore(bot.db).link(
            GUILD_ID, discord_user_id=TARGET_ID, github_user_id=OTHER_GITHUB_ID, github_login=OTHER_GITHUB_LOGIN
        )
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "link show", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert OTHER_GITHUB_LOGIN in last_message(interaction).description


# ---------------------------------------------------------------- /link remove


async def test_remove_unlinks(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await GitHubAccountStore(bot.db).link(
            GUILD_ID, discord_user_id=MODERATOR_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN
        )
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "link remove", interaction)

        remaining = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert remaining is None
    assert last_message(interaction).title == "已解除"


async def test_remove_says_when_there_was_nothing(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "link remove", interaction)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "github.remove.not_linked"


async def test_remove_of_someone_else_requires_manage_roles(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild, permissions=discord.Permissions(send_messages=True))

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "link remove", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.missing_permissions"


async def test_the_module_registers_the_link_group(settings) -> None:
    bot = await setup_bot(settings)
    try:
        group = next(command for command in bot.tree.get_commands() if command.name == "link")
        names = {child.name for child in group.commands}  # type: ignore[attr-defined]
    finally:
        await bot.db.close()

    assert names == {"github", "show", "remove"}
