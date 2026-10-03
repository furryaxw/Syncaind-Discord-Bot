"""release 推送：绑定即回填（按时间正序）、游标只前进、mention 一律禁用、超长截断、失败提示。"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import discord
import pytest

from bot.core.errors import UserError
from bot.integrations.github import ReleaseSourceError, ReleaseTargetStore
from bot.modules.github_feed import cog as feed_module
from tests.discord_fakes import (
    GUILD_ID,
    MODERATOR_ID,
    FakeInteraction,
    FakeThread,
    build_guild,
    run_command,
    setup_bot,
)
from tests.github_fakes import (
    FakeReleaseSource,
    FakeWatcherClient,
    FakeWatcherStream,
    make_event,
    make_release,
    release_payload,
)

REPO = "furryaxw/SprocketModManager"
THREAD_ID = 1555474676979859486


def add_thread(guild: Any, thread: FakeThread) -> FakeThread:
    guild.channels.append(thread)
    guild._channels[thread.id] = thread
    thread.guild = guild
    return thread


def embeds(channel: Any) -> list[discord.Embed]:
    return [sent["embed"] for sent in channel._sent]


def sent_args(channel: Any) -> list[dict[str, Any]]:
    return list(channel._sent)


async def prepare(
    settings: Any,
    *,
    releases: list[Any] | None = None,
    error: Exception | None = None,
    monkeypatch: pytest.MonkeyPatch | None = None,
    locale: str | None = "zh-CN",
) -> tuple[Any, Any, FakeThread, FakeReleaseSource]:
    if monkeypatch is not None:
        monkeypatch.setattr(feed_module, "POST_DELAY_SECONDS", 0)
    bot = await setup_bot(settings)
    if locale is not None:
        # 推出去的消息没有交互对象，语言只能取服务器配置；测试里显式配上。
        await bot.guild_settings.update(GUILD_ID, locale=locale)
    guild = build_guild()
    thread = add_thread(guild, FakeThread(THREAD_ID, name="SprocketModManager"))
    source = FakeReleaseSource(releases, error=error)
    bot.cogs["GitHubFeedCog"]._source = source
    return bot, guild, thread, source


def interaction_for(guild: Any, thread: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild, channel=thread)


def reply_embed(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._edits[-1]["embed"]


def last_message_embed(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


# ---------------------------------------------------------------- 存储层


async def test_target_lifecycle(db: Any) -> None:
    store = ReleaseTargetStore(db)

    await store.add(GUILD_ID, repo=REPO, channel_id=1, cursor_release_id=10, created_by=MODERATOR_ID)
    target = await store.get(GUILD_ID, REPO)

    assert target is not None
    assert (target.repo, target.channel_id, target.cursor_release_id) == (REPO, 1, 10)
    assert [item.repo for item in await store.all(GUILD_ID)] == [REPO]

    await store.remove(GUILD_ID, REPO)
    assert await store.get(GUILD_ID, REPO) is None


async def test_rebinding_moves_the_target(db: Any) -> None:
    store = ReleaseTargetStore(db)
    await store.add(GUILD_ID, repo=REPO, channel_id=1, cursor_release_id=10, created_by=MODERATOR_ID)

    await store.add(GUILD_ID, repo=REPO, channel_id=2, cursor_release_id=None, created_by=MODERATOR_ID)

    target = await store.get(GUILD_ID, REPO)
    assert target is not None and target.channel_id == 2 and target.cursor_release_id is None


async def test_the_cursor_only_moves_forward(db: Any) -> None:
    """游标退回去会导致重推，所以 SQL 里就把它钉住。"""
    store = ReleaseTargetStore(db)
    await store.add(GUILD_ID, repo=REPO, channel_id=1, cursor_release_id=10, created_by=MODERATOR_ID)

    await store.set_cursor(GUILD_ID, REPO, 5)

    target = await store.get(GUILD_ID, REPO)
    assert target is not None and target.cursor_release_id == 10

    await store.set_cursor(GUILD_ID, REPO, 11)
    target = await store.get(GUILD_ID, REPO)
    assert target is not None and target.cursor_release_id == 11


# ---------------------------------------------------------------- /feed add


async def test_add_backfills_oldest_first(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    releases = [
        make_release(3, repo=REPO, tag="v0.3"),
        make_release(2, repo=REPO, tag="v0.2"),
        make_release(1, repo=REPO, tag="v0.1"),
    ]
    bot, guild, thread, _source = await prepare(settings, releases=releases, monkeypatch=monkeypatch)
    try:
        interaction = interaction_for(guild, thread)
        await run_command(bot, "feed add", interaction, repo=REPO)

        target = await ReleaseTargetStore(bot.db).get(GUILD_ID, REPO)
    finally:
        await bot.db.close()

    assert [embed.title for embed in embeds(thread)] == [REPO + " v0.1", REPO + " v0.2", REPO + " v0.3"]
    assert target is not None and target.channel_id == THREAD_ID
    assert target.cursor_release_id == 3, "游标要停在新处"
    assert "回填了 **3** 条" in reply_embed(interaction).description


async def test_add_defaults_to_where_the_command_runs(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """在帖子里跑就绑那个帖子 —— 不用手输 ID。"""
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
        target = await ReleaseTargetStore(bot.db).get(GUILD_ID, REPO)
    finally:
        await bot.db.close()

    assert target is not None and target.channel_id == THREAD_ID


async def test_add_accepts_a_full_url(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=f"https://github.com/{REPO}")
    finally:
        await bot.db.close()

    assert source.calls == [REPO]


async def test_add_says_when_there_are_no_releases(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        interaction = interaction_for(guild, thread)
        await run_command(bot, "feed add", interaction, repo=REPO)
        target = await ReleaseTargetStore(bot.db).get(GUILD_ID, REPO)
    finally:
        await bot.db.close()

    assert "还没有任何 release" in reply_embed(interaction).description
    assert target is not None and target.cursor_release_id is None


async def test_add_rejects_a_malformed_repo(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "feed add", interaction_for(guild, thread), repo="not a repo")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "feed.bad_repo"


async def test_add_reports_a_fetch_failure(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(
        settings, error=ReleaseSourceError("nope", status=404, repo=REPO), monkeypatch=monkeypatch
    )
    try:
        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "feed.fetch_failed"
    assert excinfo.value.kwargs["status"] == 404


# ---------------------------------------------------------------- 外部文本的处理


async def test_every_post_disables_mentions(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """release notes 是外部输入：里面写 `@everyone` 绝不能真的 @ 一遍。"""
    body = "修复内容 @everyone @here <@&123>"
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO, body=body)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    sent = sent_args(thread)
    assert len(sent) == 1
    mentions = sent[0]["allowed_mentions"]
    assert mentions.everyone is False
    assert mentions.roles is False
    assert mentions.users is False


async def test_a_long_body_is_truncated(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    body = "\n".join(f"第 {index} 行变更说明" * 5 for index in range(300))
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO, body=body)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    description = embeds(thread)[0].description
    assert len(description) <= 4096
    assert "截断" in description


async def test_an_empty_body_says_so(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    assert "没有填写 release 说明" in embeds(thread)[0].description


async def test_a_body_that_is_only_a_marker_says_there_are_no_notes(
    settings: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """清掉机器标记后没内容了 —— 该说「没填说明」，而不是把 JSON 摊给用户看。"""
    body = '<!-- sp-compat {"hamish.sprocket": ["0.2.53.x"]} -->'
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO, body=body)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    description = embeds(thread)[0].description
    assert "sp-compat" not in description
    assert "没有填写 release 说明" in description


async def test_a_pushed_release_hides_the_metadata_marker(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """端到端：作者发布说明里那种 `<!-- sp-compat ... -->` 不该出现在帖子里。"""
    body = '## 变更\n- 修了 A\n\n<!-- sp-compat {"hamish.sprocket": ["0.2.53.x", "0.2.55.5"]} -->\n'
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO, body=body)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    description = embeds(thread)[0].description
    assert "sp-compat" not in description
    assert "hamish.sprocket" not in description
    assert description == "## 变更\n- 修了 A"


async def test_wrapper_text_follows_the_server_locale(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """没配服务器语言时，我们自己那几句包装文案（而不是 release 正文）回退到默认语言。

    推送的消息没有交互对象，拿不到客户端语言 —— 和处罚/反应角色的私信是同一条限制。
    """
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO)], monkeypatch=monkeypatch, locale=None
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    assert "no notes" in embeds(thread)[0].description


async def test_a_prerelease_is_marked(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(1, repo=REPO, prerelease=True)], monkeypatch=monkeypatch
    )
    try:
        await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    assert embeds(thread)[0].title.startswith("🧪")


class ForbiddenThread(FakeThread):
    """只覆盖 ``send``：模拟机器人在目标处没有发消息/嵌入的权限。"""

    async def send(self, **kwargs: Any) -> None:
        raise discord.Forbidden(SimpleNamespace(status=403, reason="Forbidden"), "missing permissions")


async def test_posting_without_permission_says_which_channel(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """权限不够时要指名道姓，而不是抛一个 discord.py 的异常出去。"""
    monkeypatch.setattr(feed_module, "POST_DELAY_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        thread = add_thread(guild, ForbiddenThread(THREAD_ID))
        bot.cogs["GitHubFeedCog"]._source = FakeReleaseSource([make_release(1, repo=REPO)])

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "feed add", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "feed.cannot_post"
    assert excinfo.value.kwargs["channel"] == thread.mention


# ---------------------------------------------------------------- 监听服务（SSE 订阅）


def wire_watcher(
    bot: Any,
    guild: Any,
    monkeypatch: pytest.MonkeyPatch,
    *,
    events: list[dict[str, Any]] | None = None,
    page: Any = None,
    base: str = "http://watch:1",
) -> tuple[Any, Any, Any]:
    cog = bot.cogs["GitHubFeedCog"]
    bot.settings.watcher_base_url = base
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)
    client = FakeWatcherClient(page)
    stream = FakeWatcherStream(events)
    cog._watcher = client
    cog._stream = stream
    return cog, client, stream


async def bind(bot: Any, *, repo: str = REPO, cursor: int | None = None) -> None:
    await ReleaseTargetStore(bot.db).add(
        GUILD_ID, repo=repo, channel_id=THREAD_ID, cursor_release_id=cursor, created_by=MODERATOR_ID
    )


async def test_watching_is_off_without_a_configured_service(settings: Any) -> None:
    """没配监听服务就不该起后台任务 —— 此时 /feed sync 是唯一入口。"""
    bot = await setup_bot(settings)
    try:
        cog = bot.cogs["GitHubFeedCog"]
        assert bot.settings.watcher_base_url is None
        assert cog.start_watching() is False
    finally:
        await bot.db.close()


async def test_a_stream_event_posts_and_advances_both_cursors(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        await bind(bot)
        cog, _client, stream = wire_watcher(
            bot, guild, monkeypatch, events=[make_event(5, payload=release_payload(101, tag="v0.6.1"))]
        )

        await cog._watch_once()

        target = await ReleaseTargetStore(bot.db).get(GUILD_ID, REPO)
        watcher_cursor = await cog.cursor.get()
    finally:
        await bot.db.close()

    assert [embed.title for embed in embeds(thread)] == [f"{REPO} v0.6.1"]
    assert target is not None and target.cursor_release_id == 101, "release 游标要推进"
    assert watcher_cursor == 5, "事件游标也要推进，否则重启会重放"
    assert stream.since_seen == [0]


async def test_the_subscription_resumes_from_the_saved_cursor(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, _thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        cog, _client, stream = wire_watcher(bot, guild, monkeypatch)
        await cog.cursor.set(7)

        await cog._watch_once()
    finally:
        await bot.db.close()

    assert stream.since_seen == [7]


async def test_an_event_for_an_unbound_repo_is_ignored(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        cog, _client, _stream = wire_watcher(
            bot, guild, monkeypatch, events=[make_event(5, payload=release_payload(101))]
        )

        await cog._watch_once()
    finally:
        await bot.db.close()

    assert thread._sent == []


async def test_a_replayed_release_is_not_posted_twice(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """事件可能重放（断线续传、服务重启）：去重靠 release 游标，不靠事件游标。"""
    bot, guild, thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        await bind(bot, cursor=101)
        cog, _client, _stream = wire_watcher(
            bot, guild, monkeypatch, events=[make_event(5, payload=release_payload(101))]
        )

        await cog._watch_once()
    finally:
        await bot.db.close()

    assert thread._sent == []


async def test_without_a_payload_the_notes_are_fetched_by_tag(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """监听服务没开 GHW_INCLUDE_PAYLOAD 时的路：事件只给链接，正文去 GitHub 按 tag 取。"""
    release = make_release(101, repo=REPO, tag="v0.6.1", body="## 从 GitHub 取回的正文")
    bot, guild, thread, source = await prepare(settings, releases=[release], monkeypatch=monkeypatch)
    try:
        await bind(bot)
        cog, _client, _stream = wire_watcher(bot, guild, monkeypatch, events=[make_event(6, title="v0.6.1")])

        await cog._watch_once()
    finally:
        await bot.db.close()

    assert source.tag_calls == [(REPO, "v0.6.1")]
    assert "从 GitHub 取回的正文" in embeds(thread)[0].description


async def test_eviction_triggers_a_full_reconciliation(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """SSE 不会说自己漏了事件，所以订阅前那次区间检查必须真的起作用。"""
    from bot.integrations.github.watcher import WatcherPage

    releases = [make_release(101, repo=REPO, tag="v0.6.1")]
    bot, guild, thread, _source = await prepare(settings, releases=releases, monkeypatch=monkeypatch)
    try:
        await bind(bot, cursor=None)
        page = WatcherPage(events=(), cursor=0, latest_seq=99, more=False, since_evicted=True)
        cog, _client, stream = wire_watcher(bot, guild, monkeypatch, page=page)

        await cog._watch_once()

        watcher_cursor = await cog.cursor.get()
    finally:
        await bot.db.close()

    assert [embed.title for embed in embeds(thread)] == [f"{REPO} v0.6.1"], "对账要把新的补上"
    assert watcher_cursor == 99, "对账后游标跟到最新，避免每轮都重来"
    assert stream.since_seen == [], "被挤掉时不该再去订阅（先对账）"


async def test_posting_failure_does_not_wedge_the_subscription(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """一条发不出去，不该卡住后面的事件。

    事件游标照常推进是安全的：**去重真正靠的是各目标的 release 游标**，
    所以这条没推成的 release 后面还能被 `/feed sync` 或对账补上。
    """
    monkeypatch.setattr(feed_module, "POST_DELAY_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        await bot.guild_settings.update(GUILD_ID, locale="zh-CN")
        guild = build_guild()
        thread = add_thread(guild, ForbiddenThread(THREAD_ID))
        bot.cogs["GitHubFeedCog"]._source = FakeReleaseSource([])
        await bind(bot)
        cog, _client, _stream = wire_watcher(
            bot, guild, monkeypatch, events=[make_event(5, payload=release_payload(101))]
        )

        await cog._watch_once()  # 不该抛出去

        watcher_cursor = await cog.cursor.get()
        target = await ReleaseTargetStore(bot.db).get(GUILD_ID, REPO)
    finally:
        await bot.db.close()

    assert watcher_cursor == 5
    assert target is not None and target.cursor_release_id is None, "没推成就不该推进 release 游标"
    assert thread._sent == []


# ---------------------------------------------------------------- /feed sync


async def test_sync_posts_only_what_is_newer_than_the_cursor(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    releases = [make_release(3, repo=REPO), make_release(2, repo=REPO)]
    bot, guild, thread, _source = await prepare(settings, releases=releases, monkeypatch=monkeypatch)
    try:
        interaction = interaction_for(guild, thread)
        store = ReleaseTargetStore(bot.db)
        await store.add(GUILD_ID, repo=REPO, channel_id=THREAD_ID, cursor_release_id=2, created_by=MODERATOR_ID)
        thread._sent.clear()

        await run_command(bot, "feed sync", interaction, repo=REPO)
        target = await store.get(GUILD_ID, REPO)
    finally:
        await bot.db.close()

    assert [embed.title for embed in embeds(thread)] == [f"{REPO} v1.0.0"], "只推 id=3 那条"
    assert target is not None and target.cursor_release_id == 3
    assert "推送了 **1** 条" in reply_embed(interaction).description


async def test_sync_says_nothing_when_up_to_date(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(
        settings, releases=[make_release(3, repo=REPO)], monkeypatch=monkeypatch
    )
    try:
        interaction = interaction_for(guild, thread)
        await ReleaseTargetStore(bot.db).add(
            GUILD_ID, repo=REPO, channel_id=THREAD_ID, cursor_release_id=3, created_by=MODERATOR_ID
        )

        await run_command(bot, "feed sync", interaction, repo=REPO)
    finally:
        await bot.db.close()

    assert thread._sent == []
    assert "没有比游标更新的 release" in reply_embed(interaction).description


async def test_sync_requires_a_binding(settings: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, thread, _source = await prepare(settings, releases=[], monkeypatch=monkeypatch)
    try:
        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "feed sync", interaction_for(guild, thread), repo=REPO)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "feed.not_watched"


# ---------------------------------------------------------------- /feed list & remove


async def test_list_is_empty_by_default(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = interaction_for(guild, guild.channels[0])

        await run_command(bot, "feed list", interaction)
    finally:
        await bot.db.close()

    assert "还没有订阅任何仓库" in last_message_embed(interaction).description


async def test_list_shows_the_target_and_cursor(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        thread = add_thread(guild, FakeThread(THREAD_ID, name="SprocketModManager"))
        await ReleaseTargetStore(bot.db).add(
            GUILD_ID, repo=REPO, channel_id=THREAD_ID, cursor_release_id=3, created_by=MODERATOR_ID
        )
        interaction = interaction_for(guild, thread)

        await run_command(bot, "feed list", interaction)
    finally:
        await bot.db.close()

    description = last_message_embed(interaction).description
    assert REPO in description
    assert thread.mention in description
    assert "3" in description


async def test_remove_unbinds(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        thread = add_thread(guild, FakeThread(THREAD_ID))
        await ReleaseTargetStore(bot.db).add(
            GUILD_ID, repo=REPO, channel_id=THREAD_ID, cursor_release_id=1, created_by=MODERATOR_ID
        )
        interaction = interaction_for(guild, thread)

        await run_command(bot, "feed remove", interaction, repo=REPO)

        remaining = await ReleaseTargetStore(bot.db).get(GUILD_ID, REPO)
    finally:
        await bot.db.close()

    assert remaining is None
    assert "已取消订阅" in last_message_embed(interaction).description


async def test_remove_says_when_it_was_not_watched(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "feed remove", interaction_for(guild, guild.channels[0]), repo=REPO)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "feed.not_watched"
