"""tools 命令处理器：``/serverinfo`` ``/userinfo`` ``/permissions`` 真的能跑完。

命令能注册、能同步、命令树里有它，都不代表处理器能跑 —— 属性名写错会在
调用处理器时才抛。所以这里真的调用一次处理器。
"""

from __future__ import annotations

import discord
import pytest

from bot.core.errors import UserError
from tests.discord_fakes import (
    BOT_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeChannel,
    FakeGuild,
    FakeInteraction,
    FakeMember,
    build_guild,
    run_command,
    setup_bot,
)


def last_embed(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


def field_names(embed: discord.Embed) -> set[str]:
    return {field.name for field in embed.fields}


def field_value(embed: discord.Embed, name: str) -> str:
    return next(field.value for field in embed.fields if field.name == name)


async def test_serverinfo_runs(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "serverinfo", interaction)
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert embed.title == guild.name
    assert {"服务器 ID", "成员", "频道", "角色数"} <= field_names(embed)


async def test_userinfo_runs_for_self(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "userinfo", interaction)
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert embed.title == "moderator"
    assert {"用户 ID", "最高角色", "角色"} <= field_names(embed)


async def test_userinfo_runs_for_another_member(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "userinfo", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert last_embed(interaction).title == "target"


async def test_userinfo_rejects_an_unknown_member(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        outsider = FakeMember(999, 10, name="outsider")
        interaction = FakeInteraction(user=outsider, guild=guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "userinfo", interaction)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.member_not_found"


async def test_permissions_runs_without_crashing(settings) -> None:
    """``/permissions`` 不能因为 ``Member.is_default()`` 而抛 ``AttributeError``。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild(target_position=10)
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "permissions", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert {"已拥有", "未拥有", "处罚层级判定", "可处罚的成员", "角色层级判定"} <= field_names(embed)


async def test_permissions_says_a_member_can_be_punished(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "permissions", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    assert field_value(embed, "处罚层级判定").startswith("✅")
    assert field_value(embed, "可处罚的成员") == "1 个"


async def test_permissions_explains_why_the_guild_owner_cannot_be_punished(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild(owner_id=TARGET_ID)
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "permissions", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert "服务器拥有者" in field_value(last_embed(interaction), "处罚层级判定")


async def test_permissions_says_there_is_nobody_to_punish(settings) -> None:
    """一人测试服里「没法测试 kick」的真正原因，要由机器人自己说出来。"""
    bot = await setup_bot(settings)
    try:
        guild = FakeGuild(
            owner_id=MODERATOR_ID,
            members=[
                FakeMember(MODERATOR_ID, 90, name="owner"),
                FakeMember(BOT_ID, 80, name="bot", bot=True),
            ],
            channels=[FakeChannel(1)],
        )
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "permissions", interaction)
    finally:
        await bot.db.close()

    value = field_value(last_embed(interaction), "可处罚的成员")
    assert "没有可处罚的对象" in value
    assert "第二个账号" in value


async def test_permissions_lists_granted_and_missing(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild(moderator_permissions=discord.Permissions(kick_members=True, ban_members=True))
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "permissions", interaction)
    finally:
        await bot.db.close()

    embed = last_embed(interaction)
    granted = field_value(embed, "已拥有")
    missing = field_value(embed, "未拥有")
    assert "踢出成员" in granted
    assert "封禁成员" in granted
    assert "管理服务器" in missing


async def test_permissions_omits_the_role_verdict_when_member_has_no_extra_role(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        # position=0 表示最高角色就是 @everyone
        guild.get_member(TARGET_ID).top_role = guild.get_member(TARGET_ID).roles[0]
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)

        await run_command(bot, "permissions", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert "角色层级判定" not in field_names(last_embed(interaction))
