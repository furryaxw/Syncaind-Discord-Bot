"""框架级 ``/help``：按模块分组列出命令，并按调用者的权限标注可用性。"""

from __future__ import annotations

import discord
import pytest

from bot.core.errors import UserError
from tests.discord_fakes import (
    MODERATOR_ID,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)


def last_embed(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


def field_value(embed: discord.Embed, name: str) -> str:
    return next(field.value for field in embed.fields if field.name == name)


async def make_interaction(settings, *, owner_id: int | None = MODERATOR_ID, locale: str = "zh-CN"):
    bot = await setup_bot(settings)
    guild = build_guild()
    interaction = FakeInteraction(
        user=guild.get_member(MODERATOR_ID),
        guild=guild,
        locale=locale,
        owner_id=owner_id,
    )
    return bot, guild, interaction


async def test_help_groups_commands_by_module(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        await run_command(bot, "help", interaction)
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert embed.title == "命令列表"
    names = {field.name for field in embed.fields}
    assert {"框架", "处罚与审核", "工具与诊断"} <= names
    assert "/kick" in field_value(embed, "处罚与审核")
    assert "/serverinfo" in field_value(embed, "工具与诊断")
    assert "/module list" in field_value(embed, "框架")


async def test_help_marks_owner_only_commands_for_non_owners(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings, owner_id=None)
    try:
        await run_command(bot, "help", interaction)
    finally:
        await bot.db.close()

    framework_lines = field_value(last_embed(interaction), "框架")

    # owner 之外的人看到 /module 被标成不可用，但它仍然被列出来（不藏命令）
    assert "⛔ `/module list`" in framework_lines
    # 而普通命令不带标记
    assert "⛔ `/kick`" not in field_value(last_embed(interaction), "处罚与审核")


async def test_help_marks_owner_commands_as_available_for_the_owner(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings, owner_id=MODERATOR_ID)
    try:
        await run_command(bot, "help", interaction)
    finally:
        await bot.db.close()

    assert "⛔" not in field_value(last_embed(interaction), "框架")


async def test_help_explains_a_command_with_parameters_and_permissions(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        await run_command(bot, "help", interaction, command="kick")
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert embed.title == "/kick"
    assert "把成员踢出服务器" in embed.description
    assert "`member`（必填）" in embed.description
    assert "`reason`（可选）" in embed.description
    assert "需要的服务器权限" in embed.description
    assert "踢出成员" in embed.description


async def test_help_explains_a_group_command(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        await run_command(bot, "help", interaction, command="module")
    finally:
        await bot.db.close()

    description = last_embed(interaction).description
    assert "`/module list`" in description
    assert "`/module reload`" in description
    assert "只有机器人 owner 能用" in description


async def test_help_explains_a_subcommand(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        await run_command(bot, "help", interaction, command="module list")
    finally:
        await bot.db.close()

    assert last_embed(interaction).title == "/module list"


async def test_help_falls_back_to_a_partial_match_and_shows_the_real_name(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        await run_command(bot, "help", interaction, command="serv")
    finally:
        await bot.db.close()

    assert last_embed(interaction).title == "/serverinfo"


async def test_help_rejects_an_unknown_command(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "help", interaction, command="definitely-not-a-command")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "framework.help.unknown_command"


async def test_help_is_english_for_english_clients(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings, locale="en-US")
    try:
        await run_command(bot, "help", interaction, command="kick")
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert "Kick a member from the server" in embed.description
    assert "Required server permissions:" in embed.description
    assert "Kick members" in embed.description


async def test_help_only_touches_the_command_tree(settings) -> None:
    """命令树里有什么就列什么：不含任何手工维护的清单。"""
    from bot.core.help import listable_commands

    bot, _guild, interaction = await make_interaction(settings)
    try:
        expected = {name for name, _command in listable_commands(bot.tree)}
        await run_command(bot, "help", interaction)
    finally:
        await bot.db.close()

    listed = "\n".join(field.value for field in last_embed(interaction).fields)
    for name in expected:
        assert f"`/{name}`" in listed, name


async def test_disabling_a_module_hides_its_commands_from_help(settings) -> None:
    bot, _guild, interaction = await make_interaction(settings)
    try:
        await bot.loader.set_enabled("moderation", False)
        await run_command(bot, "help", interaction)
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert "处罚与审核" not in {field.name for field in embed.fields}
    assert "/kick" not in embed.description
    assert "/serverinfo" in field_value(embed, "工具与诊断")
