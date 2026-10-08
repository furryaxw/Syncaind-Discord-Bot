"""发激活码：门禁、名额、退码、开奖。

这个模块每一步失败都有去处，测试就围绕这些去处写：
**没绑 GitHub 不许做任何 SMAS 操作** / 一人一批一枚 / 先到先得封顶不超发 /
私信失败必须退码并把名额还回去 / 开奖公告里不出现码 /
**响应先于干活发出去（3 秒窗口），响应失败一次也不许扣名额** /
**服务器黑名单与单次活动的排除角色都在资格之前生效**。
"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any

import discord
import pytest
from discord import app_commands

from bot.core.errors import UserError
from bot.integrations.github import GitHubAccountStore
from bot.integrations.smas import AccessError
from bot.integrations.smas.key_store import DRAWN, FCFS, OPEN, RAFFLE, KeyDeliveryStore, KeyDropStore
from bot.integrations.smas.keys import KEYS_NODE
from tests.discord_fakes import (
    GUILD_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)
from tests.smas_fakes import FakeAccessClient, key_inventory_row, taken_key, taken_payload

BATCH = "b1"
TEAM = "acme"
NODE = KEYS_NODE.format(team_id=TEAM)
PLAINTEXT = "SMAS6N54M5YKKEFRPBSK09GZ"
RAFFLE_CHOICE = app_commands.Choice(name="抽奖", value=RAFFLE)
FCFS_CHOICE = app_commands.Choice(name="先到先得", value=FCFS)


def interaction_for(guild: Any, member: Any = None, *, message: Any = None) -> FakeInteraction:
    interaction = FakeInteraction(user=member or guild.get_member(MODERATOR_ID), guild=guild)
    interaction.message = message
    return interaction


def configured_client() -> FakeAccessClient:
    client = FakeAccessClient()
    client.set("take", NODE, taken_payload(taken_key()))
    client.set("release", NODE, {"key_id": "key-1", "delivered_to": None})
    client.set("read", "team.system.keys", {"keys": [key_inventory_row(team_id=TEAM, batch_id=BATCH)]})
    return client


def wire(bot: Any, client: FakeAccessClient) -> Any:
    cog = bot.cogs["AccessKeysCog"]
    cog._client = client
    return cog


async def link_account(bot: Any, discord_user_id: int = MODERATOR_ID) -> None:
    """给某个 Discord 用户绑一个 GitHub 账号。

    一个 GitHub 账号只能绑一个人（store 里有唯一索引），所以按 Discord id 派生一个不同的。
    """
    await GitHubAccountStore(bot.db).link(
        GUILD_ID,
        discord_user_id=discord_user_id,
        github_user_id=discord_user_id + 1,
        github_login=f"user{discord_user_id}",
    )


def patch_lookup(bot: Any, guild: Any) -> None:
    """把 ``bot.get_user`` / ``bot.get_channel`` 接到假 guild 上。

    真实实现走缓存（命令交互里有），而离线测试的 bot 没连网关、缓存是空的；
    开奖要给中奖者私信、改活动消息要拿频道，这两处都得有人接。
    """
    bot.get_user = guild.get_member
    bot.get_channel = guild.get_channel_or_thread


async def open_drop(
    bot: Any,
    guild: Any,
    client: FakeAccessClient,
    *,
    mode: str = FCFS,
    count: int = 1,
    role: discord.Role | None = None,
    deny_role: discord.Role | None = None,
) -> Any:
    """跑一次 /key drop，返回 (drop, 那条消息)。"""
    cog = wire(bot, client)
    patch_lookup(bot, guild)
    # 公开消息/按钮/私信都取服务器语言；测试里显式配上，断言才稳定。
    await bot.guild_settings.update(GUILD_ID, locale="zh-CN")
    interaction = interaction_for(guild)
    choice = RAFFLE_CHOICE if mode == RAFFLE else FCFS_CHOICE
    await run_command(
        bot, "key drop", interaction, batch=BATCH, count=count, mode=choice, role=role, deny_role=deny_role
    )
    # 编号现在是短随机串，所以按「本 guild 里开着的那一个」取，而不是猜一个数字。
    drop = next(iter(await cog.drops.open_in_guild(GUILD_ID)), None)
    message = guild.channels[0]._messages.get(drop.message_id) if drop is not None else None
    return drop, message


def drop_button(channel: Any) -> Any:
    """发出去那条消息上的按钮。

    ``view`` 只存在于 ``send`` 的参数里 —— 真实 ``discord.Message`` 上**没有** ``.view``
    （替身纪律测试抓到过这一点），所以从 ``_sent`` 里取。
    """
    view = channel._sent[-1]["view"]
    return next(iter(view.children))


def ephemeral_text(interaction: FakeInteraction) -> str:
    """命令/按钮的回复文本：可能在 ``_messages``，也可能 defer 之后走 ``_edits``。"""
    edits = interaction.response._edits
    if edits:
        embed = edits[-1].get("embed")
        return (embed.description if embed is not None else edits[-1].get("content", "")) or ""
    last = interaction.response._messages[-1]
    embed = last.get("embed")
    return (embed.description if embed is not None else last.get("content", "")) or ""


def followup_text(interaction: FakeInteraction) -> str:
    """按钮的回执：defer 之后成功/失败走 ``edit_original_response``，报错走 ``followup.send``。"""
    if interaction.response._edits:
        return ephemeral_text(interaction)
    sent = interaction.followup._sent or interaction.response._messages
    last = sent[-1]
    embed = last.get("embed")
    return (embed.description if embed is not None else last.get("content", "")) or ""


def expired_interaction() -> discord.NotFound:
    """Discord 对「这条交互已经不存在了」回的就是这个（code 10062）：点完超过 3 秒。"""
    return discord.NotFound(
        SimpleNamespace(status=404, reason="Not Found"),
        {"message": "Unknown interaction", "code": 10062},
    )


# ---------------------------------------------------------------- 门禁：先绑 GitHub


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("key drop", {"batch": BATCH, "count": 1, "mode": FCFS_CHOICE}),
        ("key close", {"drop_id": "ABC123"}),
        ("key list", {"batch": BATCH}),
    ],
)
async def test_smas_commands_require_a_linked_github_account(settings: Any, path: str, params: dict) -> None:
    """SMAS 的身份就是 GitHub 账号 —— 没绑就不知道该把码记到谁头上。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        client = configured_client()
        wire(bot, client)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, path, interaction_for(guild), **params)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "github.link_required"
    assert client.calls == [], "门禁没过就不该去碰服务端"


async def test_the_button_also_requires_a_linked_account(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        client = configured_client()
        await link_account(bot)
        _drop, message = await open_drop(bot, guild, client)
        # 重新开一个没绑的成员来点
        target = guild.get_member(TARGET_ID)
        interaction = interaction_for(guild, target, message=message)

        await wire(bot, client).handle_button(interaction)
    finally:
        await bot.db.close()

    assert "先绑定 GitHub" in followup_text(interaction)
    assert client.actions() == ["read"], "只有开活动时查过一次批次归属，点击没有取码"


# ---------------------------------------------------------------- 开活动


async def test_a_raffle_drop_posts_a_signup_button(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        drop, message = await open_drop(bot, guild, configured_client(), mode=RAFFLE, count=2)
    finally:
        await bot.db.close()

    assert drop is not None and drop.mode == RAFFLE and drop.status == OPEN
    assert drop.closes_at is not None, "抽奖必须有截止时间（后台据此开奖）"
    assert message is not None
    button = drop_button(guild.channels[0])
    assert isinstance(button, discord.ui.Button) and button.label == "参与抽奖"


async def test_a_fcfs_drop_posts_a_claim_button(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        drop, _message = await open_drop(bot, guild, configured_client(), mode=FCFS, count=3)
    finally:
        await bot.db.close()

    assert drop is not None and drop.closes_at is None, "先到先得没有截止时间"
    assert drop_button(guild.channels[0]).label == "领取"


async def test_a_bad_batch_says_so(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        wire(bot, configured_client())

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "key drop", interaction_for(guild), batch="  ", count=1, mode=FCFS_CHOICE)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "keys.bad_batch"


# ---------------------------------------------------------------- 先到先得


async def test_a_fcfs_click_delivers_by_dm_and_never_shows_the_code(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        _drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        interaction = interaction_for(guild, member, message=message)

        await wire(bot, client).handle_button(interaction)

        deliveries = await KeyDeliveryStore(bot.db).for_batch(GUILD_ID, BATCH)
    finally:
        await bot.db.close()

    assert PLAINTEXT in member._dms[-1], "码要私信给当事人"
    assert PLAINTEXT not in ephemeral_text(interaction), "回执里不能出现完整的码"
    assert [row.discord_user_id for row in deliveries] == [MODERATOR_ID]
    _action, _node, data, _team = client.calls[-1]
    assert data["recipient"] == f"discord:{MODERATOR_ID}"


async def test_a_second_claim_is_refused_without_taking_a_key(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        _drop, message = await open_drop(bot, guild, client, mode=FCFS, count=2)
        cog = wire(bot, client)
        await cog.handle_button(interaction_for(guild, member, message=message))

        interaction = interaction_for(guild, member, message=message)
        await cog.handle_button(interaction)
    finally:
        await bot.db.close()

    assert "已经在**这一批**里领过了" in followup_text(interaction)
    assert client.actions().count("take") == 1, "第二次点击不该再去取一枚"


async def test_a_failed_dm_returns_the_key_and_the_slot(settings: Any) -> None:
    """**最关键的一条**：私信发不出去时，码与名额都要还回去。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        member._fail_dm = True
        await link_account(bot)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        cog = wire(bot, client)

        interaction = interaction_for(guild, member, message=message)
        await cog.handle_button(interaction)

        deliveries = await KeyDeliveryStore(bot.db).for_batch(GUILD_ID, BATCH)
        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert client.actions() == ["read", "take", "release"], "必须把码退回去"
    assert deliveries == [], "登记要撤掉，否则他再也领不了这一批"
    assert latest is not None and latest.delivered == 0, "名额也要还回去"
    assert "退回去了" in ephemeral_text(interaction)


async def test_a_click_defers_before_it_touches_anything(settings: Any, caplog: pytest.LogCaptureFixture) -> None:
    """响应必须在 3 秒窗口里先发出去，而且日志要留下对应的那个数字。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        _drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)

        interaction = interaction_for(guild, member, message=message)
        with caplog.at_level(logging.INFO, logger="bot.access_keys"):
            await wire(bot, client).handle_button(interaction)
    finally:
        await bot.db.close()

    assert interaction.response._deferred is True, "进来的第一件事就是 defer"
    messages = [record.getMessage() for record in caplog.records]
    assert any("从交互创建起" in text for text in messages), "要留下和那 3 秒窗口对应的数字"


async def test_an_expired_click_takes_no_slot_and_logs_one_warning(
    settings: Any, caplog: pytest.LogCaptureFixture
) -> None:
    """点完超过 3 秒时 Discord 回 10062（Unknown interaction）。

    那是交互在网络路上就过期了，不是这里的故障：**一个名额都不许扣**（响应失败之后
    没有人能替用户把名额还回去），日志也只该是一条 WARNING —— 打两段异常栈只会把真因埋掉。
    """
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        interaction = interaction_for(guild, member, message=message)
        interaction.response._defer_error = expired_interaction()

        with caplog.at_level(logging.WARNING, logger="bot.access_keys"):
            await wire(bot, client).handle_button(interaction)

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert latest is not None and latest.delivered == 0, "过期的点击不能扣名额"
    assert client.actions() == ["read"], "过期之后不该再去取码"
    assert interaction.response._messages == []
    assert interaction.followup._sent == [], "过期之后哪里都发不出去，别白试一次"
    assert any("已过期" in record.getMessage() for record in caplog.records)
    assert not any(record.exc_info for record in caplog.records), "过期不是异常，不该打栈"
    assert [record.levelno for record in caplog.records if record.levelno >= logging.ERROR] == []


async def test_an_unexpected_failure_after_the_slot_is_taken_gives_it_back(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """取名额之后的任何意外都要还名额 —— 否则会出现「一枚没发出去、活动却显示已发完」。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        cog = wire(bot, client)

        async def explode(drop: Any, user: Any) -> str:
            raise RuntimeError("boom")

        monkeypatch.setattr(cog, "_deliver_one", explode)
        interaction = interaction_for(guild, member, message=message)

        await cog.handle_button(interaction)

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert latest is not None and latest.delivered == 0, "名额必须还回去"
    assert "出了点意外" in followup_text(interaction)


async def test_a_broken_access_server_is_not_reported_as_a_dm_problem(settings: Any) -> None:
    """取码失败与私信失败是两件事：说成「你关了私信」会把排查方向带偏。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        client.set("take", NODE, AccessError("access_unreachable", "连不上"))
        drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)

        interaction = interaction_for(guild, member, message=message)
        await wire(bot, client).handle_button(interaction)

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    text = followup_text(interaction)
    assert "连不上 access server" in text
    assert "私信" not in text
    assert latest is not None and latest.delivered == 0, "取码失败也要把名额还回去"


async def test_an_exhausted_batch_is_reported_as_empty(settings: Any) -> None:
    """批次空了是状态不是故障，说法要不一样。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        client.set("take", NODE, AccessError("key_unavailable", "空了"))
        drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)

        interaction = interaction_for(guild, member, message=message)
        await wire(bot, client).handle_button(interaction)

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert "没有可发的码" in followup_text(interaction)
    assert latest is not None and latest.delivered == 0


async def test_fcfs_stops_at_the_key_count(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        await link_account(bot, TARGET_ID)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        cog = wire(bot, client)

        await cog.handle_button(interaction_for(guild, guild.get_member(MODERATOR_ID), message=message))
        second = interaction_for(guild, guild.get_member(TARGET_ID), message=message)
        await cog.handle_button(second)

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert "已经结束" in followup_text(second), "发满就把活动关掉，后到的人看到的是已结束"
    assert client.actions().count("take") == 1, "超出的点击不该取码"
    assert latest is not None and latest.status != OPEN, "发满了就该把按钮关掉"


# ---------------------------------------------------------------- 资格


async def test_a_role_gate_is_checked(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = guild.get_member(MODERATOR_ID).top_role
        await link_account(bot)  # 开活动的人自己也要绑（SMAS 操作都要）
        await link_account(bot, TARGET_ID)
        client = configured_client()
        _drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1, role=role)
        cog = wire(bot, client)

        # TARGET 没有这个角色 → 拒绝；不该取码
        denied = interaction_for(guild, guild.get_member(TARGET_ID), message=message)
        await cog.handle_button(denied)
        actions_after_deny = list(client.actions())
    finally:
        await bot.db.close()

    assert "只限" in followup_text(denied)
    assert "take" not in actions_after_deny


# ---------------------------------------------------------------- 抽奖


async def test_entering_a_raffle_twice_is_refused(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=RAFFLE, count=1)
        cog = wire(bot, client)

        await cog.handle_button(interaction_for(guild, member, message=message))
        second = interaction_for(guild, member, message=message)
        await cog.handle_button(second)

        entries = await cog.drops.entries(drop.drop_id)
    finally:
        await bot.db.close()

    assert entries == [MODERATOR_ID]
    assert "已经在抽奖名单里" in followup_text(second)


async def test_drawing_picks_winners_and_the_announcement_has_no_code(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        winner = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        await link_account(bot, TARGET_ID)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=RAFFLE, count=1)
        cog = wire(bot, client)
        await cog.drops.enter(drop.drop_id, MODERATOR_ID)

        await cog.draw(await cog.drops.get(drop.drop_id))

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert latest is not None and latest.status == DRAWN
    assert PLAINTEXT in winner._dms[-1], "中奖者要私信收到码"
    announcement = message.embeds[0]
    assert f"<@{MODERATOR_ID}>" in announcement.fields[-1].value
    assert PLAINTEXT not in announcement.fields[-1].value, "公告里不能出现码"


async def test_drawing_with_no_entries_takes_nothing(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=RAFFLE, count=1)
        cog = wire(bot, client)

        await cog.draw(await cog.drops.get(drop.drop_id))

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert latest is not None and latest.status == DRAWN
    assert client.actions() == ["read"], "没人报名就不该取码"
    assert "一枚都没能发出去" in message.embeds[0].fields[-1].value


async def test_the_sweeper_draws_only_due_raffles(settings: Any) -> None:
    """开奖靠库里的状态驱动 —— 重启也不会漏掉该开活动。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        client = configured_client()
        drop, _message = await open_drop(bot, guild, client, mode=RAFFLE, count=1)
        cog = wire(bot, client)
        await cog.drops.enter(drop.drop_id, MODERATOR_ID)
        # 把截止时间改到过去
        await bot.db.execute(
            "UPDATE key_drops SET closes_at = '2000-01-01T00:00:00+00:00' WHERE drop_id = ?", (drop.drop_id,)
        )

        drawn = await cog.draw_due_drops()

        latest = await KeyDropStore(bot.db).get(drop.drop_id)
    finally:
        await bot.db.close()

    assert drawn == 1
    assert latest is not None and latest.status == DRAWN


# ---------------------------------------------------------------- /key list


async def test_list_shows_prefixes_never_full_codes(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link_account(bot)
        await KeyDeliveryStore(bot.db).claim(
            GUILD_ID, batch_id=BATCH, discord_user_id=MODERATOR_ID, key_id="key-1", key_prefix=PLAINTEXT[:10]
        )
        interaction = interaction_for(guild)

        await run_command(bot, "key list", interaction, batch=BATCH)
    finally:
        await bot.db.close()

    description = interaction.response._messages[-1]["embed"].description
    assert PLAINTEXT not in description
    assert PLAINTEXT[:10] in description


async def test_the_module_registers_its_commands(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        group = next(command for command in bot.tree.get_commands() if command.name == "key")
        names = {child.name for child in group.commands}  # type: ignore[attr-defined]
    finally:
        await bot.db.close()

    assert names == {"drop", "close", "list", "deny-role", "allow-role"}


# ---------------------------------------------------------------- 黑名单


async def test_a_denied_role_cannot_claim(settings: Any) -> None:
    """黑名单压过白名单：被排除的人连点按钮都不该取码。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        _drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        cog = wire(bot, client)
        await cog.denied.add(GUILD_ID, member.top_role.id, created_by=MODERATOR_ID)

        interaction = interaction_for(guild, member, message=message)
        await cog.handle_button(interaction)
    finally:
        await bot.db.close()

    assert "黑名单" in followup_text(interaction)
    assert client.actions() == ["read"], "被排除的人不该取码"


async def test_a_denied_role_also_blocks_raffle_signup(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(MODERATOR_ID)
        await link_account(bot)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=RAFFLE, count=1)
        cog = wire(bot, client)
        await cog.denied.add(GUILD_ID, member.top_role.id, created_by=MODERATOR_ID)

        interaction = interaction_for(guild, member, message=message)
        await cog.handle_button(interaction)

        entries = await cog.drops.entries(drop.drop_id)
    finally:
        await bot.db.close()

    assert "黑名单" in followup_text(interaction)
    assert entries == []


async def test_the_deny_list_can_be_managed(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = guild.get_member(MODERATOR_ID).top_role
        await link_account(bot)

        denied_interaction = interaction_for(guild)
        await run_command(bot, "key deny-role", denied_interaction, role=role)
        after_add = await wire(bot, configured_client()).denied.all(GUILD_ID)

        allowed_interaction = interaction_for(guild)
        await run_command(bot, "key allow-role", allowed_interaction, role=role)
        after_remove = await wire(bot, configured_client()).denied.all(GUILD_ID)
    finally:
        await bot.db.close()

    assert after_add == {role.id}
    assert after_remove == set()
    assert role.mention in ephemeral_text(denied_interaction), "回执要带上当前名单"


# ---------------------------------------------------------------- 指令级黑名单


async def test_a_role_excluded_from_one_drop_only_loses_that_drop(settings: Any) -> None:
    """`deny_role` 只对这一次活动生效：公开消息写出来，别人照常领，他换一个活动也能领。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(TARGET_ID)
        excluded = member.top_role
        await link_account(bot)
        await link_account(bot, TARGET_ID)
        client = configured_client()
        _first, first_message = await open_drop(bot, guild, client, mode=FCFS, count=1, deny_role=excluded)
        cog = wire(bot, client)

        refused = interaction_for(guild, member, message=first_message)
        await cog.handle_button(refused)
        actions_after_refusal = list(client.actions())

        await cog.handle_button(interaction_for(guild, guild.get_member(MODERATOR_ID), message=first_message))

        _second, second_message = await open_drop(bot, guild, client, mode=FCFS, count=1)
        await cog.handle_button(interaction_for(guild, member, message=second_message))
    finally:
        await bot.db.close()

    assert "排除了" in followup_text(refused)
    assert "take" not in actions_after_refusal, "被这次活动排除的人不该取码"
    assert PLAINTEXT in guild.get_member(MODERATOR_ID)._dms[-1], "活动里的排除不影响别人"
    assert PLAINTEXT in member._dms[-1], "换一个没排除他的活动照常能领"
    assert "已排除" in first_message.embeds[0].description and excluded.mention in first_message.embeds[0].description
    assert "已排除" not in second_message.embeds[0].description, "没设排除就不该冒出这一句"


async def test_a_role_excluded_from_one_drop_also_blocks_raffle_signup(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        member = guild.get_member(TARGET_ID)
        await link_account(bot)
        await link_account(bot, TARGET_ID)
        client = configured_client()
        drop, message = await open_drop(bot, guild, client, mode=RAFFLE, count=1, deny_role=member.top_role)
        cog = wire(bot, client)

        interaction = interaction_for(guild, member, message=message)
        await cog.handle_button(interaction)

        entries = await cog.drops.entries(drop.drop_id)
    finally:
        await bot.db.close()

    assert "排除了" in followup_text(interaction)
    assert entries == [], "被这次活动排除的人连报名都不该进名单"


async def test_the_server_deny_list_and_the_drop_exclusion_are_two_layers(settings: Any) -> None:
    """两层各拦各的：全局名单说黑名单，活动自己的排除说排除，都压在白名单之前。"""
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        moderator = guild.get_member(MODERATOR_ID)
        target = guild.get_member(TARGET_ID)
        await link_account(bot)
        await link_account(bot, TARGET_ID)
        client = configured_client()
        _drop, message = await open_drop(bot, guild, client, mode=FCFS, count=1, deny_role=target.top_role)
        cog = wire(bot, client)
        await cog.denied.add(GUILD_ID, moderator.top_role.id, created_by=MODERATOR_ID)

        blocked_by_list = interaction_for(guild, moderator, message=message)
        await cog.handle_button(blocked_by_list)
        blocked_by_drop = interaction_for(guild, target, message=message)
        await cog.handle_button(blocked_by_drop)
    finally:
        await bot.db.close()

    assert "黑名单" in followup_text(blocked_by_list)
    assert "排除了" in followup_text(blocked_by_drop)
    assert client.actions() == ["read"], "两边都被拦住，一枚都不该取"


async def test_a_role_cannot_be_both_the_requirement_and_the_exclusion(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        role = guild.get_member(MODERATOR_ID).top_role
        await link_account(bot)
        cog = wire(bot, configured_client())
        interaction = interaction_for(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(
                bot,
                "key drop",
                interaction,
                batch=BATCH,
                count=1,
                mode=FCFS_CHOICE,
                role=role,
                deny_role=role,
            )
        drops = await cog.drops.open_in_guild(GUILD_ID)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "keys.drop.deny_conflict"
    assert drops == [], "自相矛盾的活动不该建出来"
