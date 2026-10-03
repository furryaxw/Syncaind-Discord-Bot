"""roles：颜色/成员列表解析，以及 /role 建/删/改名/改色/权限位/批量授予与移除。"""

from __future__ import annotations

from typing import Any

import discord
import pytest

from bot.core.errors import UserError
from bot.modules.roles import cog as roles_cog_module
from bot.modules.roles.cog import MemberParseError, parse_colour, parse_member_ids
from tests.discord_fakes import (
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    FakeMember,
    FakeRole,
    build_guild,
    run_command,
    setup_bot,
)

ROLE_ID = 4242
OTHER_MEMBER_ID = 999


def make_interaction(guild: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)


def add_role(guild: Any, *, role_id: int = ROLE_ID, position: int = 5, **kwargs: Any) -> FakeRole:
    role = FakeRole(role_id, position, name=kwargs.pop("name", "临时角色"), **kwargs)
    guild.roles.append(role)
    return role


def stub_confirmation(monkeypatch: pytest.MonkeyPatch, *, answer: bool) -> None:
    async def fake_ask(interaction: Any, *, embed: Any, view: Any) -> bool:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.result = answer
        return answer

    monkeypatch.setattr(roles_cog_module, "ask_confirmation", fake_ask)


def last_message(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


def last_edit(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._edits[-1]["embed"]


# ---------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    ("text", "expected"),
    [
        ("ff0000", 0xFF0000),
        ("#ff0000", 0xFF0000),
        ("0xFF0000", 0xFF0000),
        ("f00", 0xFF0000),
        ("  #FF8800  ", 0xFF8800),
        ("0000ff", 0x0000FF),
    ],
)
def test_parse_colour_accepts_common_forms(text: str, expected: int) -> None:
    assert parse_colour(text).value == expected


@pytest.mark.parametrize("text", ["blue", "#12345", "gggggg", "", "#1234567"])
def test_parse_colour_rejects_garbage(text: str) -> None:
    with pytest.raises(ValueError):
        parse_colour(text)


def test_parse_member_ids_handles_mentions_and_plain_ids() -> None:
    assert parse_member_ids("<@111> 222, <@!333>") == (111, 222, 333)


def test_parse_member_ids_accepts_chinese_separators() -> None:
    assert parse_member_ids("111，222、333") == (111, 222, 333)


def test_parse_member_ids_deduplicates_and_keeps_order() -> None:
    assert parse_member_ids("222 111 222") == (222, 111)


def test_parse_member_ids_of_nothing_is_empty() -> None:
    assert parse_member_ids("") == ()
    assert parse_member_ids("   ") == ()


def test_parse_member_ids_rejects_anything_else() -> None:
    with pytest.raises(MemberParseError) as excinfo:
        parse_member_ids("111 不是ID")

    assert excinfo.value.value == "不是ID"


# ---------------------------------------------------------------- 建 / 删 / 改名 / 改色


async def test_create_role_passes_everything_through(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(
            bot,
            "role create",
            interaction,
            name="活动组织者",
            color="ff8800",
            hoist=True,
            mentionable=True,
            allow="manage_messages",
            deny="administrator",
        )
    finally:
        await bot.db.close()

    role = guild._created_roles[-1]
    assert role.name == "活动组织者"
    assert role.colour.value == 0xFF8800
    assert role.hoist is True
    assert role.mentionable is True
    assert role.permissions.manage_messages is True
    assert role.permissions.administrator is False
    assert "已创建角色" in last_message(interaction).description


async def test_create_role_rejects_a_bad_colour(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "role create", interaction, name="x", color="blue")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "roles.bad_color"


async def test_delete_role_needs_confirmation(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=False)

        await run_command(bot, "role delete", interaction, role=role, reason=None)
    finally:
        await bot.db.close()

    assert role._deleted is False
    assert last_edit(interaction).title == "已取消，什么都没做。"


async def test_delete_role_after_confirmation(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(bot, "role delete", interaction, role=role, reason="不再需要")
    finally:
        await bot.db.close()

    assert role._deleted is True
    assert "已删除角色" in last_edit(interaction).description


async def test_rename_and_recolour(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)

        await run_command(bot, "role rename", interaction, role=role, name="新名字")
        await run_command(bot, "role color", interaction, role=role, value="#00ff00")
    finally:
        await bot.db.close()

    assert role._edits[0]["name"] == "新名字"
    assert role.colour.value == 0x00FF00
    assert "颜色已改为" in last_message(interaction).description


# ---------------------------------------------------------------- 权限位


async def test_permissions_only_touches_the_named_flags(settings) -> None:
    """只改点名的那几项：没提到的权限必须原样保留，否则一次调用就会清掉别的权限。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild, permissions=discord.Permissions(send_messages=True, view_channel=True))
        interaction = make_interaction(guild)

        await run_command(
            bot, "role permissions", interaction, role=role, allow="manage_messages", deny="send_messages"
        )
    finally:
        await bot.db.close()

    updated = role._edits[-1]["permissions"]
    assert updated.manage_messages is True
    assert updated.send_messages is False
    assert updated.view_channel is True, "没点名的权限必须保持原样"


async def test_permissions_requires_at_least_one_flag(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "role permissions", interaction, role=role, allow=None, deny=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.no_permissions_given"


async def test_unknown_permission_names_are_rejected(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "role permissions", interaction, role=role, allow="not_a_flag", deny=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.unknown_permissions"


# ---------------------------------------------------------------- 层级


async def test_a_role_above_the_bot_is_refused(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        # moderator 的最高角色位置 90，高于机器人的 80
        role = guild.get_member(MODERATOR_ID).top_role
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "role rename", interaction, role=role, name="越权")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.hierarchy.role_above_bot"
    assert role._edits == []


async def test_a_managed_role_is_refused(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild, managed=True)
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "role delete", interaction, role=role, reason=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.role_managed_by_integration"


# ---------------------------------------------------------------- 批量授予 / 移除


async def test_grant_adds_the_role_to_several_members(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild(extra_members=[FakeMember(OTHER_MEMBER_ID, 10, name="other")])
        role = add_role(guild)
        interaction = make_interaction(guild)
        members = f"{guild.get_member(TARGET_ID).mention} <@{OTHER_MEMBER_ID}>"

        await run_command(bot, "role grant", interaction, role=role, members=members)
    finally:
        await bot.db.close()

    assert f"add_role:{role.id}" in guild.get_member(TARGET_ID)._calls
    assert f"add_role:{role.id}" in guild.get_member(OTHER_MEMBER_ID)._calls
    assert "变更 2 人" in last_message(interaction).description


async def test_grant_skips_members_who_already_have_the_role(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = guild.get_role(TARGET_ID * 10)  # 目标成员本来就有这个角色
        interaction = make_interaction(guild)

        await run_command(bot, "role grant", interaction, role=role, members=str(TARGET_ID))
    finally:
        await bot.db.close()

    assert guild.get_member(TARGET_ID)._calls == []
    assert "变更 0 人" in last_message(interaction).description
    assert "无需变更 1 人" in last_message(interaction).description


async def test_grant_reports_members_it_cannot_find(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)

        await run_command(bot, "role grant", interaction, role=role, members="123456789")
    finally:
        await bot.db.close()

    embed = last_message(interaction)
    assert any("123456789" in field.value for field in embed.fields)


async def test_grant_rejects_a_non_numeric_list(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "role grant", interaction, role=role, members="把大家都加进来")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "roles.bad_member"


async def test_remove_takes_the_role_back(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = guild.get_role(TARGET_ID * 10)  # 目标成员本来就有
        interaction = make_interaction(guild)

        await run_command(bot, "role revoke", interaction, role=role, members=str(TARGET_ID))
    finally:
        await bot.db.close()

    assert f"remove_role:{role.id}" in guild.get_member(TARGET_ID)._calls
    assert "移除完成" in last_message(interaction).title


# ---------------------------------------------------------------- /role info


async def test_info_lists_key_permissions(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(
            guild,
            permissions=discord.Permissions(manage_messages=True, kick_members=True),
            hoist=True,
        )
        interaction = make_interaction(guild)

        await run_command(bot, "role info", interaction, role=role)
    finally:
        await bot.db.close()

    embed = last_message(interaction)
    values = {field.name: field.value for field in embed.fields}
    assert "管理消息" in values["关键权限"]
    assert "踢出成员" in values["关键权限"]
    assert "管理员" not in values["关键权限"]
    assert values["分组显示"] == "是"
