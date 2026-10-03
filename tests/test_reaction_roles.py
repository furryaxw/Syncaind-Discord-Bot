"""reaction_roles：映射落库，以及反应事件真的给/收角色。"""

from __future__ import annotations

from typing import Any

import discord
import pytest

from bot.core.errors import UserError
from bot.modules.reaction_roles.service import ReactionRoleService
from tests.discord_fakes import (
    GUILD_ID,
    MESSAGE_ID,
    MOD_LOG_CHANNEL_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    FakeMember,
    FakePayload,
    FakeRole,
    add_message,
    build_guild,
    run_command,
    setup_bot,
)

ROLE_ID = 4242
OTHER_GUILD_ID = 999


def make_interaction(guild: Any, **kwargs: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild, **kwargs)


def add_role(guild: Any, *, role_id: int = ROLE_ID, position: int = 5, **kwargs: Any) -> FakeRole:
    role = FakeRole(role_id, position, name=kwargs.pop("name", "反应角色"), **kwargs)
    guild.roles.append(role)
    return role


async def seed_mapping(bot: Any, *, emoji: str = "👍", role_id: int = ROLE_ID, message_id: int = MESSAGE_ID) -> None:
    await ReactionRoleService(bot.db).add(
        GUILD_ID,
        message_id=message_id,
        channel_id=MOD_LOG_CHANNEL_ID,
        emoji=emoji,
        role_id=role_id,
        created_by=MODERATOR_ID,
    )


def patch_guild(bot: Any, guild: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """让 ``bot.get_guild`` 返回假服务器——离线状态下它本来拿不到任何 guild。"""
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)


def last_message(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


# ---------------------------------------------------------------- service


async def test_mapping_round_trip(db) -> None:
    service = ReactionRoleService(db)
    await service.add(
        GUILD_ID,
        message_id=MESSAGE_ID,
        channel_id=MOD_LOG_CHANNEL_ID,
        emoji="👍",
        role_id=ROLE_ID,
        created_by=MODERATOR_ID,
    )

    assert await service.role_id_for(GUILD_ID, MESSAGE_ID, "👍") == ROLE_ID
    assert await service.role_id_for(GUILD_ID, MESSAGE_ID, "👎") is None
    mapping = (await service.for_message(GUILD_ID, MESSAGE_ID))[0]
    assert mapping.jump_url.endswith(f"/{GUILD_ID}/{MOD_LOG_CHANNEL_ID}/{MESSAGE_ID}")


async def test_adding_the_same_emoji_twice_replaces_the_role(db) -> None:
    service = ReactionRoleService(db)
    for role_id in (ROLE_ID, ROLE_ID + 1):
        await service.add(
            GUILD_ID,
            message_id=MESSAGE_ID,
            channel_id=MOD_LOG_CHANNEL_ID,
            emoji="👍",
            role_id=role_id,
            created_by=MODERATOR_ID,
        )

    mappings = await service.for_message(GUILD_ID, MESSAGE_ID)

    assert len(mappings) == 1
    assert mappings[0].role_id == ROLE_ID + 1


async def test_clear_and_role_cleanup(db) -> None:
    service = ReactionRoleService(db)
    for message_id, emoji, role_id in (
        (MESSAGE_ID, "👍", ROLE_ID),
        (MESSAGE_ID + 1, "🎉", ROLE_ID + 1),
    ):
        await service.add(
            GUILD_ID,
            message_id=message_id,
            channel_id=MOD_LOG_CHANNEL_ID,
            emoji=emoji,
            role_id=role_id,
            created_by=MODERATOR_ID,
        )

    assert await service.remove_all_for_role(GUILD_ID, ROLE_ID) == 1
    assert await service.clear_message(GUILD_ID, MESSAGE_ID + 1) == 1
    assert await service.for_guild(GUILD_ID) == []


# ---------------------------------------------------------------- 配置命令


async def test_add_stores_a_mapping(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        add_message(channel)
        role = add_role(guild)
        interaction = make_interaction(guild, channel=channel)

        await run_command(bot, "reactionrole add", interaction, message_id=str(MESSAGE_ID), emoji="👍", role=role)

        mappings = await ReactionRoleService(bot.db).for_message(GUILD_ID, MESSAGE_ID)
    finally:
        await bot.db.close()

    assert [mapping.role_id for mapping in mappings] == [ROLE_ID]
    assert "已绑定" in last_message(interaction).description


async def test_add_accepts_a_custom_emoji_and_normalises_it(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        add_message(channel)
        role = add_role(guild)
        interaction = make_interaction(guild, channel=channel)

        await run_command(
            bot, "reactionrole add", interaction, message_id=str(MESSAGE_ID), emoji="<:party:123>", role=role
        )

        mapping = (await ReactionRoleService(bot.db).for_message(GUILD_ID, MESSAGE_ID))[0]
    finally:
        await bot.db.close()

    assert mapping.emoji == "<:party:123>"


async def test_add_rejects_a_bad_emoji(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        add_message(channel)
        role = add_role(guild)
        interaction = make_interaction(guild, channel=channel)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "reactionrole add", interaction, message_id=str(MESSAGE_ID), emoji="", role=role)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "reaction_roles.bad_emoji"


async def test_add_rejects_a_message_that_does_not_exist(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild, channel=guild.channels[0])

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "reactionrole add", interaction, message_id="123", emoji="👍", role=role)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "reaction_roles.message_not_found"


async def test_add_rejects_a_non_numeric_message_id(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        interaction = make_interaction(guild, channel=guild.channels[0])

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "reactionrole add", interaction, message_id="看这条", emoji="👍", role=role)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "reaction_roles.bad_message_id"


async def test_add_refuses_when_the_bot_cannot_manage_roles(settings) -> None:
    """配置时就该拦住：机器人没有 Manage Roles 的话，之后每个反应都会失败。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        add_message(channel)
        role = add_role(guild)
        interaction = make_interaction(guild, channel=channel, app_permissions=discord.Permissions(send_messages=True))

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "reactionrole add", interaction, message_id=str(MESSAGE_ID), emoji="👍", role=role)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "reaction_roles.bot_missing_manage_roles"


async def test_add_refuses_a_role_above_the_bot(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        channel = guild.channels[0]
        add_message(channel)
        role = guild.get_member(MODERATOR_ID).top_role  # 位置 90 > 机器人的 80
        interaction = make_interaction(guild, channel=channel)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "reactionrole add", interaction, message_id=str(MESSAGE_ID), emoji="👍", role=role)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "errors.hierarchy.role_above_bot"


async def test_remove_deletes_the_mapping(settings) -> None:
    bot = await setup_bot(settings)
    try:
        await seed_mapping(bot)
        guild = build_guild()
        interaction = make_interaction(guild, channel=guild.channels[0])

        await run_command(bot, "reactionrole remove", interaction, message_id=str(MESSAGE_ID), emoji="👍")

        remaining = await ReactionRoleService(bot.db).for_message(GUILD_ID, MESSAGE_ID)
    finally:
        await bot.db.close()

    assert remaining == []
    assert "已解绑" in last_message(interaction).description


async def test_remove_reports_a_missing_binding(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild, channel=guild.channels[0])

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "reactionrole remove", interaction, message_id=str(MESSAGE_ID), emoji="👍")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "reaction_roles.remove.not_found"


async def test_list_shows_bindings_and_marks_deleted_roles(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        await seed_mapping(bot)
        interaction = make_interaction(guild, channel=guild.channels[0])

        await run_command(bot, "reactionrole list", interaction, message_id=None)

        embed = last_message(interaction)
        assert embed.fields[0].name == "👍"
        assert role.mention in embed.fields[0].value
    finally:
        await bot.db.close()


async def test_list_is_empty_at_first(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = make_interaction(guild, channel=guild.channels[0])

        await run_command(bot, "reactionrole list", interaction, message_id=None)
    finally:
        await bot.db.close()

    assert last_message(interaction).description == "没有任何绑定。"


async def test_clear_removes_every_binding_of_a_message(settings) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        add_role(guild)
        await seed_mapping(bot, emoji="👍")
        await seed_mapping(bot, emoji="🎉", role_id=ROLE_ID)
        interaction = make_interaction(guild, channel=guild.channels[0])

        await run_command(bot, "reactionrole clear", interaction, message_id=str(MESSAGE_ID))

        remaining = await ReactionRoleService(bot.db).for_message(GUILD_ID, MESSAGE_ID)
    finally:
        await bot.db.close()

    assert remaining == []
    assert "已清掉 2 条绑定" in last_message(interaction).description


# ---------------------------------------------------------------- 事件


async def test_reaction_grants_the_role_and_tells_the_member(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await bot.guild_settings.update(GUILD_ID, locale="zh-CN")
        guild = build_guild()
        role = add_role(guild)
        target = guild.get_member(TARGET_ID)
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(FakePayload(user_id=TARGET_ID, member=target, emoji="👍"))
    finally:
        await bot.db.close()

    # 点表情本身没有任何界面回应，所以要主动私信当事人一句
    assert target._calls == [f"add_role:{role.id}", "dm"]
    assert "获得了角色" in target._dms[-1]
    assert role.name in target._dms[-1]


async def test_reaction_removal_takes_the_role_back_and_tells_the_member(
    settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    bot = await setup_bot(settings)
    try:
        await bot.guild_settings.update(GUILD_ID, locale="zh-CN")
        guild = build_guild()
        role = add_role(guild)
        target = guild.get_member(TARGET_ID)
        await target.add_roles(role)
        target._calls.clear()
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        # 原始移除事件里 payload.member 是 None，要靠 user_id 反查
        await cog.on_raw_reaction_remove(FakePayload(user_id=TARGET_ID, member=None, emoji="👍"))
    finally:
        await bot.db.close()

    assert target._calls == [f"remove_role:{role.id}", "dm"]
    assert "失去了角色" in target._dms[-1]


async def test_notification_falls_back_to_the_default_locale(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """反应事件里没有客户端语言，只能回退到 DEFAULT_LOCALE——这条把回退行为钉住。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        target = guild.get_member(TARGET_ID)
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(FakePayload(user_id=TARGET_ID, member=target, emoji="👍"))
    finally:
        await bot.db.close()

    assert "You now have the role" in target._dms[-1]
    assert role.name in target._dms[-1]


async def test_nothing_is_said_when_the_role_is_already_in_place(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """没有变化就不打扰：连 Discord 都能省一次没用的调用。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = add_role(guild)
        target = guild.get_member(TARGET_ID)
        await target.add_roles(role)
        target._calls.clear()
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(FakePayload(user_id=TARGET_ID, member=target, emoji="👍"))
    finally:
        await bot.db.close()

    assert target._calls == []


async def test_unbound_emoji_is_ignored(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        add_role(guild)
        target = guild.get_member(TARGET_ID)
        await seed_mapping(bot, emoji="👍")
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(FakePayload(user_id=TARGET_ID, member=target, emoji="👎"))
    finally:
        await bot.db.close()

    assert target._calls == []


async def test_bot_reactions_are_ignored(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        add_role(guild)
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]
        robot = FakeMember(777, 10, name="other-bot", bot=True)

        await cog.on_raw_reaction_add(FakePayload(user_id=777, member=robot, emoji="👍"))
    finally:
        await bot.db.close()

    assert robot._calls == []


async def test_events_from_another_guild_are_ignored(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        add_role(guild)
        target = guild.get_member(TARGET_ID)
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(
            FakePayload(user_id=TARGET_ID, member=target, emoji="👍", guild_id=OTHER_GUILD_ID)
        )
    finally:
        await bot.db.close()

    assert target._calls == []


async def test_a_deleted_role_cleans_up_its_rules(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """角色没了就顺手清掉规则，别留下指向空气的绑定。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()  # 故意不把 ROLE_ID 加进 guild.roles
        target = guild.get_member(TARGET_ID)
        await seed_mapping(bot)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(FakePayload(user_id=TARGET_ID, member=target, emoji="👍"))

        remaining = await ReactionRoleService(bot.db).for_guild(GUILD_ID)
    finally:
        await bot.db.close()

    assert remaining == []
    assert target._calls == []


async def test_a_role_above_the_bot_is_not_granted(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        await bot.guild_settings.update(GUILD_ID, locale="zh-CN")
        guild = build_guild()
        role = guild.get_member(MODERATOR_ID).top_role  # 位置 90
        target = guild.get_member(TARGET_ID)
        await seed_mapping(bot, role_id=role.id)
        patch_guild(bot, guild, monkeypatch)
        cog = bot.cogs["ReactionRolesCog"]

        await cog.on_raw_reaction_add(FakePayload(user_id=TARGET_ID, member=target, emoji="👍"))
    finally:
        await bot.db.close()

    # 没能给上角色时也要告诉当事人，否则他只会觉得「点了没反应」
    assert target._calls == ["dm"]
    assert "没能给你角色" in target._dms[-1]
