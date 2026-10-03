"""channels：权限名解析，以及 /channel 建/删/改名/锁定/慢速/权限覆盖。"""

from __future__ import annotations

from typing import Any

import discord
import pytest

from bot.core.errors import UserError
from bot.core.permission_flags import (
    PermissionParseError,
    build_overwrite,
    inherit_overwrite,
    parse_flags,
)
from bot.modules.channels import cog as channels_cog_module
from tests.discord_fakes import (
    MODERATOR_ID,
    TARGET_ID,
    FakeCategory,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)


def make_interaction(guild: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)


def stub_confirmation(monkeypatch: pytest.MonkeyPatch, *, answer: bool) -> None:
    async def fake_ask(interaction: Any, *, embed: Any, view: Any) -> bool:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.result = answer
        return answer

    monkeypatch.setattr(channels_cog_module, "ask_confirmation", fake_ask)


def last_edit(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._edits[-1]["embed"]


def last_message(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


# ---------------------------------------------------------------- 纯函数


def test_parse_flags_basic() -> None:
    assert parse_flags("send_messages, view_channel") == ("send_messages", "view_channel")


def test_parse_flags_accepts_chinese_separators() -> None:
    assert parse_flags("send_messages，view_channel、attach_files") == (
        "send_messages",
        "view_channel",
        "attach_files",
    )


def test_parse_flags_deduplicates_and_keeps_order() -> None:
    assert parse_flags("view_channel send_messages view_channel") == ("view_channel", "send_messages")


def test_parse_flags_of_nothing_is_empty() -> None:
    assert parse_flags(None) == ()
    assert parse_flags("   ") == ()


def test_parse_flags_rejects_unknown_names() -> None:
    with pytest.raises(PermissionParseError) as excinfo:
        parse_flags("send_messages, definitely_not_a_flag")

    assert excinfo.value.unknown == ("definitely_not_a_flag",)


def test_build_overwrite_deny_wins() -> None:
    overwrite = build_overwrite(("send_messages",), ("send_messages",))

    assert overwrite.send_messages is False


def test_build_overwrite_sets_both_sides() -> None:
    overwrite = build_overwrite(("view_channel",), ("send_messages",))

    assert overwrite.view_channel is True
    assert overwrite.send_messages is False
    assert overwrite.manage_messages is None


def test_inherit_overwrite_clears_the_flag() -> None:
    overwrite = inherit_overwrite("send_messages")

    assert overwrite.send_messages is None


# ---------------------------------------------------------------- 建 / 删 / 改名


async def test_create_text_channel(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "channel create", interaction, name="新频道", kind="text", topic="话题")
    finally:
        await bot.db.close()

    created = guild._created_channels[-1]
    assert created["kind"] == "text"
    assert created["name"] == "新频道"
    assert created["kwargs"]["topic"] == "话题"
    assert "已创建" in last_message(interaction).description


async def test_create_private_channel_hides_it_from_everyone(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "channel create", interaction, name="秘密", kind="text", private=True)
    finally:
        await bot.db.close()

    overwrites = guild._created_channels[-1]["kwargs"]["overwrites"]
    assert overwrites[guild.default_role].view_channel is False


async def test_create_public_channel_sets_no_overwrites(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "channel create", interaction, name="公开", kind="text")
    finally:
        await bot.db.close()

    assert guild._created_channels[-1]["kwargs"]["overwrites"] is None


async def test_create_category(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "channel create", interaction, name="分类", kind="category")
    finally:
        await bot.db.close()

    assert guild._created_channels[-1]["kind"] == "category"


async def test_create_voice_channel(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "channel create", interaction, name="语音", kind="voice")
    finally:
        await bot.db.close()

    assert guild._created_channels[-1]["kind"] == "voice"


async def test_delete_needs_confirmation(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=False)

        await run_command(bot, "channel delete", interaction, channel=channel, reason=None)
    finally:
        await bot.db.close()

    assert channel._deleted is False
    assert last_edit(interaction).title == "已取消，什么都没做。"


async def test_delete_runs_after_confirmation(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(bot, "channel delete", interaction, channel=channel, reason="合并频道")
    finally:
        await bot.db.close()

    assert channel._deleted is True
    assert "已删除" in last_edit(interaction).description


async def test_rename_channel(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)

        await run_command(bot, "channel rename", interaction, channel=channel, name="闲聊")
    finally:
        await bot.db.close()

    assert channel.name == "闲聊"
    assert channel._edits[0]["name"] == "闲聊"


# ---------------------------------------------------------------- 锁定 / 慢速


async def test_lock_denies_send_messages_for_everyone(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)

        await run_command(bot, "channel lock", interaction, channel=channel)
    finally:
        await bot.db.close()

    call = channel._permissions[-1]
    assert call["target"] is guild.default_role
    assert call["send_messages"] is False
    assert call["send_messages_in_threads"] is False


async def test_unlock_clears_the_flag_instead_of_allowing_it(settings) -> None:
    """解锁是「撤掉限制」而不是「强制允许」：必须置 None，让上层规则继续生效。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)

        await run_command(bot, "channel unlock", interaction, channel=channel)
    finally:
        await bot.db.close()

    call = channel._permissions[-1]
    assert call["target"] is guild.default_role
    assert call["overwrite"].send_messages is None
    assert call["overwrite"].send_messages_in_threads is None


async def test_slowmode_sets_the_delay(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)

        await run_command(bot, "channel slowmode", interaction, channel=channel, seconds=30)
    finally:
        await bot.db.close()

    assert channel._edits[0]["slowmode_delay"] == 30
    assert "30 秒" in last_message(interaction).description


async def test_slowmode_zero_clears_it(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)

        await run_command(bot, "channel slowmode", interaction, channel=channel, seconds=0)
    finally:
        await bot.db.close()

    assert channel._edits[0]["slowmode_delay"] == 0
    assert last_message(interaction).description == "已关闭 <#555> 的慢速模式。"


# ---------------------------------------------------------------- 权限覆盖


async def test_overwrite_rejects_unknown_permission_names(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(
                bot,
                "channel overwrite",
                interaction,
                channel=guild.channels[0],
                target=guild.default_role,
                allow="send_messagest",  # 手滑打错一个字母
                deny=None,
            )
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.unknown_permissions"
    assert "send_messagest" in excinfo.value.kwargs["permissions"]


async def test_overwrite_requires_at_least_one_permission(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(
                bot,
                "channel overwrite",
                interaction,
                channel=guild.channels[0],
                target=guild.default_role,
                allow=None,
                deny=None,
            )
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.no_permissions_given"


async def test_overwrite_applies_after_confirmation(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(
            bot,
            "channel overwrite",
            interaction,
            channel=channel,
            target=guild.get_role(TARGET_ID * 10),
            allow="view_channel",
            deny="send_messages",
        )
    finally:
        await bot.db.close()

    call = channel._permissions[-1]
    assert call["overwrite"].view_channel is True
    assert call["overwrite"].send_messages is False
    assert "设置权限覆盖" in last_edit(interaction).description


async def test_overwrite_applies_to_every_channel_in_the_category(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)
        category = await guild.create_category("测试分类")
        first = await guild.create_text_channel("一", category=category)
        second = await guild.create_text_channel("二", category=category)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(
            bot,
            "channel overwrite-category",
            interaction,
            category=category,
            target=guild.default_role,
            allow="view_channel",
            deny=None,
        )
    finally:
        await bot.db.close()

    for channel in (first, second):
        call = channel._permissions[-1]
        assert call["target"] is guild.default_role
        assert call["overwrite"].view_channel is True
    assert "2 个频道" in last_edit(interaction).description


async def test_overwrite_category_reports_when_it_is_empty(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)
        category = FakeCategory(9999, name="空分类")
        stub_confirmation(monkeypatch, answer=True)

        await run_command(
            bot,
            "channel overwrite-category",
            interaction,
            category=category,
            target=guild.default_role,
            allow="view_channel",
            deny=None,
        )
    finally:
        await bot.db.close()

    assert "0 个频道" in last_edit(interaction).description


async def test_channel_commands_are_registered_under_the_group(settings) -> None:
    bot = await setup_bot(settings)
    try:
        group = next(command for command in bot.tree.get_commands() if command.name == "channel")
        names = {child.name for child in group.commands}  # type: ignore[attr-defined]
    finally:
        await bot.db.close()

    assert names == {
        "create",
        "delete",
        "rename",
        "lock",
        "unlock",
        "slowmode",
        "overwrite",
        "overwrite-category",
    }
