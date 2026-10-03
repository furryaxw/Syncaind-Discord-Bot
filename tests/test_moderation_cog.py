"""moderation 命令处理器：确认 → 执行 → 落库 → mod-log 的完整链路。

这一层直接调用命令的底层协程（``Command._callback``），跳过 Discord 的权限检查——
权限位由 Discord 自己保证，而角色层级是机器人自己的判断，后者在 test_checks 里逐分支测过。
确认交互本身在 test_ui 里单独测过，所以这里把 ``ask_confirmation`` 换成可控替身。
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.core.bot import SyncaindBot
from bot.core.errors import UserError
from bot.modules.moderation import cog as moderation_cog_module
from tests.discord_fakes import (
    GUILD_ID,
    MOD_LOG_CHANNEL_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)


def make_interaction(guild: Any) -> FakeInteraction:
    """以 moderator 身份发起的交互。"""
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)


def stub_confirmation(monkeypatch: pytest.MonkeyPatch, *, answer: bool) -> list[Any]:
    """把确认交互换成可控替身，并记录它收到的 view。"""
    seen: list[Any] = []

    async def fake_ask(interaction: Any, *, embed: Any, view: Any) -> bool:
        seen.append(view)
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.result = answer
        return answer

    monkeypatch.setattr(moderation_cog_module, "ask_confirmation", fake_ask)
    return seen


async def fetch_actions(bot: SyncaindBot) -> list[Any]:
    return await bot.db.fetchall("SELECT * FROM moderation_actions ORDER BY case_number")


async def test_kick_applies_records_and_logs(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        views = stub_confirmation(monkeypatch, answer=True)
        guild = build_guild()
        interaction = make_interaction(guild)
        target = guild.get_member(TARGET_ID)

        await run_command(bot, "kick", interaction, member=target, reason="刷屏")

        actions = await fetch_actions(bot)
    finally:
        await bot.db.close()

    assert target._calls[0] == "kick"
    assert len(actions) == 1
    assert actions[0]["action"] == "kick"
    assert actions[0]["target_id"] == TARGET_ID
    assert actions[0]["moderator_id"] == MODERATOR_ID
    assert actions[0]["reason"] == "刷屏"
    assert actions[0]["case_number"] == 1

    # 确认卡片绑定了发起者
    assert views[0].author_id == MODERATOR_ID
    # 成功回复里带 case 号
    assert "#1" in interaction.response._edits[-1]["embed"].description
    # mod-log 频道收到了处罚记录
    log_channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    assert len(log_channel._sent) == 1
    assert log_channel._sent[0]["embed"].title == "处罚记录"


async def test_declining_the_confirmation_changes_nothing(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        stub_confirmation(monkeypatch, answer=False)
        guild = build_guild()
        interaction = make_interaction(guild)
        target = guild.get_member(TARGET_ID)

        await run_command(bot, "ban", interaction, member=target, reason=None)

        actions = await fetch_actions(bot)
    finally:
        await bot.db.close()

    assert target._calls == []
    assert actions == []
    assert interaction.response._edits[-1]["embed"].title == "已取消，什么都没做。"


async def test_hierarchy_violation_is_refused_before_any_action(settings) -> None:
    bot = await setup_bot(settings)
    try:
        # 目标角色比机器人还高
        guild = build_guild(target_position=95)
        interaction = make_interaction(guild)
        target = guild.get_member(TARGET_ID)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "kick", interaction, member=target, reason=None)

        actions = await fetch_actions(bot)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.hierarchy.bot_role_too_low"
    assert target._calls == []
    assert actions == []
    assert interaction.response._messages == []


async def test_guild_owner_cannot_be_targeted(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild(owner_id=TARGET_ID)
        interaction = make_interaction(guild)
        target = guild.get_member(TARGET_ID)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "timeout", interaction, member=target, minutes=10, reason=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.hierarchy.target_is_owner"
    assert target._timeout_for is None


async def test_warning_escalates_at_the_threshold(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        target = guild.get_member(TARGET_ID)
        interaction = make_interaction(guild)

        for index in range(3):
            await run_command(bot, "warn", interaction, member=target, reason=f"第 {index + 1} 次")

        actions = await fetch_actions(bot)
        warnings = await bot.db.fetchall("SELECT * FROM warnings ORDER BY id")
    finally:
        await bot.db.close()

    kinds = [row["action"] for row in actions]
    assert kinds == ["warn", "warn", "warn", "auto_timeout"]
    assert len(warnings) == 3
    # 默认阈值 3 / 默认动作 timeout / 默认 60 分钟
    assert target._timeout_for is not None
    assert target._timeout_for.total_seconds() == 3600
    assert actions[-1]["automated"] == 1


async def test_warn_below_threshold_does_not_escalate(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        target = guild.get_member(TARGET_ID)
        interaction = make_interaction(guild)

        await run_command(bot, "warn", interaction, member=target, reason=None)
        await run_command(bot, "warn", interaction, member=target, reason=None)

        actions = await fetch_actions(bot)
    finally:
        await bot.db.close()

    assert [row["action"] for row in actions] == ["warn", "warn"]
    assert target._timeout_for is None


async def test_warning_count_is_reported_to_the_moderator(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        target = guild.get_member(TARGET_ID)
        interaction = make_interaction(guild)

        await run_command(bot, "warn", interaction, member=target, reason=None)

        description = interaction.response._edits[-1]["embed"].description
    finally:
        await bot.db.close()

    assert "1 / 阈值 3" in description


async def test_threshold_zero_disables_escalation(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await bot.guild_settings.update(GUILD_ID, warn_threshold=0)
        guild = build_guild()
        target = guild.get_member(TARGET_ID)
        interaction = make_interaction(guild)

        for _ in range(4):
            await run_command(bot, "warn", interaction, member=target, reason=None)

        actions = await fetch_actions(bot)
    finally:
        await bot.db.close()

    assert {row["action"] for row in actions} == {"warn"}
    assert target._timeout_for is None


async def test_escalation_can_kick(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await bot.guild_settings.update(GUILD_ID, warn_action="kick", warn_threshold=1)
        guild = build_guild()
        target = guild.get_member(TARGET_ID)
        interaction = make_interaction(guild)

        await run_command(bot, "warn", interaction, member=target, reason=None)

        actions = await fetch_actions(bot)
    finally:
        await bot.db.close()

    assert [row["action"] for row in actions] == ["warn", "auto_kick"]
    assert "kick" in target._calls


async def test_unban_rejects_a_user_that_is_not_banned(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "unban", interaction, user_id="999", reason=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "moderation.unban.not_banned"


async def test_unban_rejects_a_non_numeric_id(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "unban", interaction, user_id="not-an-id", reason=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "moderation.invalid_user_id"


async def test_reason_is_trimmed_and_capped(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        stub_confirmation(monkeypatch, answer=True)
        guild = build_guild()
        interaction = make_interaction(guild)
        target = guild.get_member(TARGET_ID)

        await run_command(bot, "kick", interaction, member=target, reason="  " + "x" * 600 + "  ")

        row = (await fetch_actions(bot))[0]
    finally:
        await bot.db.close()

    assert row["reason"].startswith("x")
    assert len(row["reason"]) == 480


async def test_dm_is_attempted_for_the_target(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """处罚之后 DM 通知当事人：先执行处罚，再通知。"""
    bot = await setup_bot(settings)
    try:
        stub_confirmation(monkeypatch, answer=True)
        guild = build_guild()
        interaction = make_interaction(guild)
        target = guild.get_member(TARGET_ID)

        await run_command(bot, "kick", interaction, member=target, reason="刷屏")
    finally:
        await bot.db.close()

    assert target._calls == ["kick", "dm"]
