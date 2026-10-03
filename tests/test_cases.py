"""cases：处罚史的查询、单条案件的查看、以及撤销（含真实的 Discord 侧回滚）。"""

from __future__ import annotations

from typing import Any

import discord
import pytest

from bot.core.bot import SyncaindBot
from bot.core.errors import UserError
from bot.core.store import GuildSettingsStore
from bot.modules.cases import cog as cases_cog_module
from bot.modules.cases.service import (
    CaseAlreadyRevoked,
    CaseNotFound,
    CaseService,
    required_permission,
    undo_plan,
)
from bot.modules.moderation.service import ModerationService
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

OTHER_TARGET_ID = 321


def make_interaction(guild: Any, **kwargs: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild, **kwargs)


async def seed_case(
    bot: SyncaindBot,
    *,
    action: str = "kick",
    target_id: int = TARGET_ID,
    reason: str | None = "刷屏",
    duration_seconds: int | None = None,
):
    service = ModerationService(bot.db, bot.guild_settings)
    return await service.record_action(
        GUILD_ID,
        action=action,
        target_id=target_id,
        moderator_id=MODERATOR_ID,
        reason=reason,
        duration_seconds=duration_seconds,
    )


def stub_confirmation(monkeypatch: pytest.MonkeyPatch, *, answer: bool) -> None:
    async def fake_ask(interaction: Any, *, embed: Any, view: Any) -> bool:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
        view.result = answer
        return answer

    monkeypatch.setattr(cases_cog_module, "ask_confirmation", fake_ask)


def last_edit(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._edits[-1]["embed"]


# ---------------------------------------------------------------- 纯函数


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("ban", "unban"),
        ("auto_ban", "unban"),
        ("timeout", "untimeout"),
        ("auto_timeout", "untimeout"),
        ("warn", "clear_warning"),
        ("kick", "none"),
        ("auto_kick", "none"),
        ("purge", "none"),
        ("something_new", "none"),
    ],
)
def test_undo_plan(action: str, expected: str) -> None:
    assert undo_plan(action) == expected


@pytest.mark.parametrize(
    ("action", "expected"),
    [
        ("ban", "ban_members"),
        ("auto_ban", "ban_members"),
        ("kick", "kick_members"),
        ("timeout", "moderate_members"),
        ("warn", "moderate_members"),
        ("purge", "manage_messages"),
        ("something_new", "moderate_members"),
    ],
)
def test_required_permission(action: str, expected: str) -> None:
    assert required_permission(action) == expected


# ---------------------------------------------------------------- service


async def test_history_is_newest_first_and_scoped_to_the_member(db) -> None:
    service = ModerationService(db, GuildSettingsStore(db))
    cases = CaseService(db)
    await service.record_action(GUILD_ID, action="warn", target_id=TARGET_ID, moderator_id=MODERATOR_ID)
    await service.record_action(GUILD_ID, action="kick", target_id=TARGET_ID, moderator_id=MODERATOR_ID)
    await service.record_action(GUILD_ID, action="ban", target_id=OTHER_TARGET_ID, moderator_id=MODERATOR_ID)

    history = await cases.history(GUILD_ID, TARGET_ID)

    assert [record.action for record in history] == ["kick", "warn"]
    assert [record.case_number for record in history] == [2, 1]


async def test_history_respects_the_limit(db) -> None:
    service = ModerationService(db, GuildSettingsStore(db))
    cases = CaseService(db)
    for _ in range(4):
        await service.record_action(GUILD_ID, action="warn", target_id=TARGET_ID, moderator_id=MODERATOR_ID)

    history = await cases.history(GUILD_ID, TARGET_ID, limit=2)

    assert len(history) == 2
    assert history[0].case_number == 4


async def test_history_is_empty_for_a_clean_member(db) -> None:
    assert await CaseService(db).history(GUILD_ID, OTHER_TARGET_ID) == []


async def test_revoke_marks_the_case_and_needs_one_existing(db) -> None:
    service = ModerationService(db, GuildSettingsStore(db))
    cases = CaseService(db)
    await service.record_action(GUILD_ID, action="kick", target_id=TARGET_ID, moderator_id=MODERATOR_ID)

    revoked = await cases.revoke(GUILD_ID, 1, moderator_id=MODERATOR_ID, reason="误判")

    assert revoked.is_revoked
    assert revoked.revoked_by == MODERATOR_ID
    assert revoked.revoke_reason == "误判"

    with pytest.raises(CaseNotFound):
        await cases.revoke(GUILD_ID, 99, moderator_id=MODERATOR_ID)

    with pytest.raises(CaseAlreadyRevoked):
        await cases.revoke(GUILD_ID, 1, moderator_id=MODERATOR_ID)


async def test_revoking_a_warning_stops_it_counting(db) -> None:
    service = ModerationService(db, GuildSettingsStore(db))
    cases = CaseService(db)
    record, _active = await service.add_warning(GUILD_ID, target_id=TARGET_ID, moderator_id=MODERATOR_ID)
    assert await service.active_warning_count(GUILD_ID, TARGET_ID) == 1

    await cases.revoke(GUILD_ID, record.case_number, moderator_id=MODERATOR_ID)

    assert await service.active_warning_count(GUILD_ID, TARGET_ID) == 0


# ---------------------------------------------------------------- /history


async def test_history_command_lists_records(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="warn", reason="刷屏")
        await seed_case(bot, action="kick", reason="屡教不改")
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "history", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    embed = interaction.response._messages[-1]["embed"]
    assert embed.title == "target 的处罚史"
    names = [field.name for field in embed.fields]
    assert names == ["#2 · 踢出", "#1 · 警告"]
    assert "屡教不改" in embed.fields[0].value


async def test_history_command_says_when_there_is_nothing(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "history", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert interaction.response._messages[-1]["embed"].description == "这个人没有任何处罚记录。"


# ---------------------------------------------------------------- /case view


async def test_case_view_shows_the_details(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="timeout", reason="刷屏", duration_seconds=600)
        guild = build_guild()
        interaction = make_interaction(guild)

        await run_command(bot, "case view", interaction, number=1)
    finally:
        await bot.db.close()

    embed = interaction.response._messages[-1]["embed"]
    assert embed.title == "case #1"
    assert embed.description == "禁言"
    values = {field.name: field.value for field in embed.fields}
    assert values["时长"] == "10 分钟"
    assert values["状态"] == "✅ 生效中"
    assert values["理由"] == "刷屏"


async def test_case_view_rejects_an_unknown_number(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "case view", interaction, number=42)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "cases.not_found"


# ---------------------------------------------------------------- /case revoke


async def test_revoke_unbans_through_discord(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="ban", reason="广告")
        guild = build_guild()
        guild._banned.add(TARGET_ID)
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(bot, "case revoke", interaction, number=1, reason="申诉通过")
    finally:
        await bot.db.close()

    assert len(guild._unbans) == 1
    assert guild._unbans[0]["user"].id == TARGET_ID
    assert "撤销 case #1" in guild._unbans[0]["reason"]
    assert "已解除对" in last_edit(interaction).description
    # mod-log 也收到了撤销记录
    log_channel = guild.get_channel(MOD_LOG_CHANNEL_ID)
    assert [call["embed"].title for call in log_channel._sent] == ["撤销记录"]


async def test_revoke_lifts_a_timeout(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="timeout", duration_seconds=600)
        guild = build_guild()
        target = guild.get_member(TARGET_ID)
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(bot, "case revoke", interaction, number=1, reason=None)
    finally:
        await bot.db.close()

    assert target._calls == ["timeout"]
    assert target._timeout_for is None
    assert "已解除" in last_edit(interaction).description


async def test_revoke_of_a_kick_only_records_it(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="kick")
        guild = build_guild()
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)

        await run_command(bot, "case revoke", interaction, number=1, reason=None)
    finally:
        await bot.db.close()

    assert "无法自动回滚" in last_edit(interaction).description


async def test_revoke_requires_confirmation(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="ban")
        guild = build_guild()
        guild._banned.add(TARGET_ID)
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=False)

        await run_command(bot, "case revoke", interaction, number=1, reason=None)

        record = await CaseService(bot.db).get(GUILD_ID, 1)
    finally:
        await bot.db.close()

    assert guild._unbans == []
    assert record is not None and not record.is_revoked
    assert last_edit(interaction).title == "已取消，什么都没做。"


async def test_revoke_checks_the_permission_for_that_action(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """能禁言不代表能解封：撤销 ban 需要 ban_members。"""
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="ban")
        guild = build_guild()
        guild._banned.add(TARGET_ID)
        interaction = make_interaction(guild, permissions=discord.Permissions(moderate_members=True))
        stub_confirmation(monkeypatch, answer=True)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "case revoke", interaction, number=1, reason=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "cases.revoke.missing_permission"
    assert "封禁成员" in excinfo.value.kwargs["permission"]
    assert guild._unbans == []


async def test_revoke_twice_is_refused(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_case(bot, action="timeout")
        guild = build_guild()
        interaction = make_interaction(guild)
        stub_confirmation(monkeypatch, answer=True)
        await run_command(bot, "case revoke", interaction, number=1, reason=None)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "case revoke", interaction, number=1, reason=None)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "cases.already_revoked"
