"""github_feed 的命令：`/feed add|list|remove|sync`。

`add` = 绑定 + **回填历史**；`sync` = 只推游标之后的（就是将来轮询/监听服务要调的那段逻辑，
所以它单独成方法 `sync_target`，两种入口共用一份实现）。
"""

from __future__ import annotations

import asyncio
import logging
import re
from typing import Any

import aiohttp
import discord
from discord import app_commands
from discord.ext import commands

from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import info_embed, ok_embed
from bot.integrations.github import (
    AiohttpTransport,
    GitHubReleaseSource,
    Release,
    ReleaseSource,
    ReleaseSourceError,
    ReleaseTarget,
    ReleaseTargetStore,
    WatcherClient,
    WatcherCursorStore,
    WatcherError,
    WatcherEvent,
    WatcherSource,
    WatcherStream,
    WatcherStreamSource,
    release_from_event,
    tag_from_url,
)

from .format import Translator, build_release_embed, release_kwargs

LOGGER = logging.getLogger("bot.github_feed")

# 回填时每条之间停一下：一个仓库可能有 20 条，连着发会撞频道路径的限流。
POST_DELAY_SECONDS = 0.4
REPO_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
# 订阅断开后的重连：正常结束等 2 秒；异常按服务端 retry 翻倍，上限 60 秒。
FLOOR_RECONNECT_SECONDS = 2.0
MAX_RECONNECT_SECONDS = 60.0

# 能当推送目标的东西：帖子（Thread）与文本频道。
FeedTarget = discord.TextChannel | discord.Thread


@app_commands.guild_only()
class GitHubFeedCog(commands.Cog):
    """把 GitHub release notes 推到帖子或频道。"""

    feed_group = app_commands.Group(
        name="feed",
        description=localized("Push GitHub release notes into a thread", "commands.feed.description"),
    )

    def __init__(
        self,
        bot: Any,
        store: ReleaseTargetStore,
        *,
        source: ReleaseSource | None = None,
        watcher: WatcherSource | None = None,
        cursor: WatcherCursorStore | None = None,
        transport: Any | None = None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.cursor = cursor or WatcherCursorStore(bot.db, logger=bot.log.getChild("github_feed"))
        self._source = source
        self._watcher = watcher
        self._stream: WatcherStreamSource | None = None
        self._transport = transport
        self._session: aiohttp.ClientSession | None = None
        self._watch_task: asyncio.Task[None] | None = None

    async def cog_unload(self) -> None:
        self.stop_watching()
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ /feed add

    @feed_group.command(
        name="add",
        description=localized("Watch a repository and backfill its releases here", "commands.feed.add.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(
        repo=localized("Repository as owner/name", "commands.feed.param_repo"),
        target=localized("Where to post; defaults to where you run this", "commands.feed.param_target"),
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def feed_add(
        self,
        interaction: discord.Interaction,
        repo: str,
        target: FeedTarget | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        normalized = _normalize_repo(repo)
        channel = self._resolve_target(interaction, target)

        # 回填可能几十秒，先 defer。
        await interaction.response.defer(ephemeral=True)

        releases = await self._fetch(normalized)
        posted = await self._post_all(channel, list(reversed(releases)))
        newest = max((release.release_id for release in releases), default=None)
        await self.store.add(
            guild.id,
            repo=normalized,
            channel_id=channel.id,
            cursor_release_id=newest,
            created_by=interaction.user.id,
        )

        t = self.bot.t
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "feed.add.title"),
                t(
                    interaction,
                    "feed.add.done" if posted else "feed.add.no_releases",
                    repo=normalized,
                    channel=channel.mention,
                    count=posted,
                ),
            )
        )

    # ------------------------------------------------------------------ /feed list

    @feed_group.command(
        name="list",
        description=localized("List watched repositories", "commands.feed.list.description"),
    )
    async def feed_list(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        targets = await self.store.all(guild.id)
        t = self.bot.t
        if not targets:
            await interaction.response.send_message(
                embed=info_embed(t(interaction, "feed.list.title"), t(interaction, "feed.list.empty")),
                ephemeral=True,
            )
            return

        lines = [
            t(
                interaction,
                "feed.list.entry",
                repo=target.repo,
                channel=_channel_mention(guild, target.channel_id),
                cursor=target.cursor_release_id or t(interaction, "feed.list.no_cursor"),
            )
            for target in targets
        ]
        lines.append("")
        lines.append(await self._watch_status(interaction))
        await interaction.response.send_message(
            embed=info_embed(t(interaction, "feed.list.title"), "\n".join(lines)),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /feed remove

    @feed_group.command(
        name="remove",
        description=localized("Stop watching a repository", "commands.feed.remove.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(repo=localized("Repository as owner/name", "commands.feed.param_repo"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def feed_remove(self, interaction: discord.Interaction, repo: str) -> None:
        guild = self._require_guild(interaction)
        normalized = _normalize_repo(repo)
        if not await self.store.remove(guild.id, normalized):
            raise UserError("feed.not_watched", repo=normalized)

        t = self.bot.t
        await interaction.response.send_message(
            embed=ok_embed(t(interaction, "feed.remove.title"), t(interaction, "feed.remove.done", repo=normalized)),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /feed sync

    @feed_group.command(
        name="sync",
        description=localized("Post releases newer than the cursor", "commands.feed.sync.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(repo=localized("Repository as owner/name", "commands.feed.param_repo"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def feed_sync(self, interaction: discord.Interaction, repo: str) -> None:
        guild = self._require_guild(interaction)
        normalized = _normalize_repo(repo)
        target = await self.store.get(guild.id, normalized)
        if target is None:
            raise UserError("feed.not_watched", repo=normalized)

        channel = guild.get_channel_or_thread(target.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            raise UserError("feed.channel_gone", channel=target.channel_id)

        await interaction.response.defer(ephemeral=True)
        posted = await self.sync_target(target, channel)
        t = self.bot.t
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "feed.sync.title"),
                t(
                    interaction,
                    "feed.sync.done" if posted else "feed.sync.nothing",
                    repo=normalized,
                    count=posted,
                ),
            )
        )

    # ------------------------------------------------------------------ 常驻订阅（监听服务）

    def start_watching(self) -> bool:
        """配了监听服务就起一个常驻订阅任务。没配返回 ``False``（此时靠 `/feed sync` 手动推）。"""
        if not self.bot.settings.watcher_base_url:
            LOGGER.info("未配置 WATCHER_BASE_URL，release 推送靠 /feed sync 手动触发")
            return False
        if self._watch_task is not None and not self._watch_task.done():
            return True
        self._watch_task = asyncio.create_task(self._watch_loop())
        return True

    def stop_watching(self) -> None:
        if self._watch_task is not None and not self._watch_task.done():
            self._watch_task.cancel()
        self._watch_task = None

    async def _watch_loop(self) -> None:
        """一条长连接，断了就退避重连。

        退避基准用服务端帧里给的 ``retry:``（默认 3 秒），逐次翻倍、上限 60 秒；
        订阅正常结束（服务端主动关）也不打转，等 2 秒再连。
        """
        failures = 0
        # 就绪之前 guild 缓存还是空的，这时候订阅只会拿到「拿不到 guild」，
        # 往启动日志里刷几条毫无信息量的警告。
        await self.bot.wait_until_ready()
        LOGGER.info("开始订阅监听服务：%s", self.bot.settings.watcher_base_url)
        while True:
            try:
                await self._watch_once()
                failures = 0
            except asyncio.CancelledError:
                raise
            except (asyncio.TimeoutError, aiohttp.ClientError, WatcherError) as exc:
                # 超时、对端关闭、服务端拒绝订阅都属于这条长连接的**正常路径**。
                # 打堆栈只会把日志淹掉，真正需要堆栈的异常反而看不见了。
                failures += 1
                LOGGER.warning("监听服务订阅中断（连续第 %d 次）：%s", failures, exc)
            except Exception:
                failures += 1
                LOGGER.exception("监听服务订阅异常（连续第 %d 次）", failures)

            if failures == 0:
                delay = FLOOR_RECONNECT_SECONDS
            else:
                base = max(1.0, self._watcher_stream().retry_ms / 1000)
                delay = min(base * (2 ** (failures - 1)), float(MAX_RECONNECT_SECONDS))
                LOGGER.info("%.0f 秒后重连监听服务", delay)
            await asyncio.sleep(delay)

    async def _watch_once(self) -> None:
        """一次订阅：先做区间检查，再进流；流结束或断开就返回，由循环决定重连。"""
        guild = self.bot.get_guild(self.bot.settings.guild_id)
        if guild is None:
            # 抛出去而不是直接返回：直接返回会被当成「正常结束」，于是每 2 秒重试一次、
            # 每 2 秒刷一条警告。这是「暂时订阅不了」，该走退避。
            raise WatcherError(f"机器人还不在 GUILD_ID={self.bot.settings.guild_id} 这个服务器里，暂不订阅")

        since = await self.cursor.get()
        # SSE 不会告诉我「游标已经被缓冲挤掉」，所以先用轮询面确认一次：
        # 一次请求换一个准确的「你可能漏了事件」信号，值得。
        page = await self._watcher_client().fetch(since, limit=1)
        if page.since_evicted:
            LOGGER.warning("监听服务的缓冲已挤掉游标 %s，改做一次全量对账", since)
            posted = await self.reconcile(guild)
            LOGGER.info("全量对账完成，补推了 %d 条 release", posted)
            await self.cursor.set(page.latest_seq)
            return

        async for event in self._watcher_stream().events(since):
            try:
                await self._handle_event(guild, event)
            except Exception:
                # 单条事件处理失败不该掐断整条订阅 —— 记下来继续。
                LOGGER.exception("处理监听事件失败：seq=%s repo=%s", event.seq, event.full_name)
            await self.cursor.set(event.seq)

    async def _handle_event(self, guild: discord.Guild, event: WatcherEvent) -> int:
        target = await self.store.get(guild.id, event.full_name)
        if target is None:
            return 0

        release = release_from_event(event)
        if release is None:
            release = await self._release_by_tag(event)
        if release is None:
            return 0
        if target.cursor_release_id is not None and release.release_id <= target.cursor_release_id:
            return 0  # 已经推过（事件重放或同一条 release 又来了）

        channel = guild.get_channel_or_thread(target.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            LOGGER.warning("%s 的目标频道 %s 找不到了，跳过这条", target.repo, target.channel_id)
            return 0

        t = await self._translator(channel)
        await self._post(channel, release, t)
        await self.store.set_cursor(guild.id, target.repo, release.release_id)
        return 1

    async def _release_by_tag(self, event: WatcherEvent) -> Release | None:
        """监听服务没带 payload 时（没开 GHW_INCLUDE_PAYLOAD），去 GitHub 按 tag 把正文换回来。

        tag 从事件链接里取：release 的 html_url 一定以 ``/releases/tag/<tag>`` 结尾。
        """
        tag = tag_from_url(event.url)
        if not tag:
            LOGGER.warning("事件里既没有 payload 也没有可解析的 tag：%s", event.url)
            return None
        try:
            return await self._source_or_create().get_release(event.full_name, tag)
        except ReleaseSourceError as exc:
            LOGGER.warning("按 tag %s 取 %s 的 release 失败：%s", tag, event.full_name, exc)
            return None

    async def reconcile(self, guild: discord.Guild) -> int:
        """全量对账：把每个已订阅仓库里比游标新的 release 都推一遍。"""
        posted = 0
        for target in await self.store.all(guild.id):
            channel = guild.get_channel_or_thread(target.channel_id)
            if not isinstance(channel, discord.abc.Messageable):
                LOGGER.warning("%s 的目标频道 %s 找不到了，跳过对账", target.repo, target.channel_id)
                continue
            try:
                posted += await self.sync_target(target, channel)
            except (ReleaseSourceError, UserError) as exc:
                LOGGER.warning("%s 对账失败：%s", target.repo, exc)
        return posted

    def _watcher_client(self) -> WatcherSource:
        if self._watcher is None:
            self._watcher = WatcherClient(
                str(self.bot.settings.watcher_base_url),
                AiohttpTransport(self._session_or_create()),
                token=self.bot.settings.watcher_api_token,
                logger=self.bot.log.getChild("github_feed"),
            )
        return self._watcher

    def _watcher_stream(self) -> WatcherStreamSource:
        if self._stream is None:
            self._stream = WatcherStream(
                str(self.bot.settings.watcher_base_url),
                token=self.bot.settings.watcher_api_token,
                session=self._session_or_create(),
                logger=self.bot.log.getChild("github_feed"),
            )
        return self._stream

    # ------------------------------------------------------------------ 共用逻辑

    async def sync_target(self, target: ReleaseTarget, channel: Any) -> int:
        """把游标之后的 release 推出去，返回推了几条。

        将来的轮询或监听服务直接调这个方法即可——**取数、判新旧、推送、推游标只有这一份实现**。
        """
        releases = await self._fetch(target.repo)
        cursor = target.cursor_release_id or 0
        pending = [release for release in releases if release.release_id > cursor]
        if not pending:
            return 0

        posted = await self._post_all(channel, list(reversed(pending)))
        await self.store.set_cursor(target.guild_id, target.repo, max(r.release_id for r in pending))
        return posted

    async def _fetch(self, repo: str) -> list[Release]:
        try:
            return await self._source_or_create().list_releases(repo)
        except ReleaseSourceError as exc:
            LOGGER.warning("取 %s 的 release 失败：%s", repo, exc)
            raise UserError("feed.fetch_failed", repo=repo, status=exc.status or "?") from exc

    async def _post_all(self, channel: Any, releases: list[Release]) -> int:
        """按给定顺序逐条推送。传进来的应当**从旧到新**，这样帖子里读起来是正序。"""
        t = await self._translator(channel)
        posted = 0
        for release in releases:
            await self._post(channel, release, t)
            posted += 1
            await asyncio.sleep(POST_DELAY_SECONDS)
        return posted

    async def _post(self, channel: Any, release: Release, t: Translator) -> None:
        embed = build_release_embed(release, t=t)
        try:
            await channel.send(embed=embed, **release_kwargs())
        except discord.Forbidden as exc:
            raise UserError("feed.cannot_post", channel=getattr(channel, "mention", channel.id)) from exc
        LOGGER.info("已推送 release：%s %s → channel %s", release.repo, release.tag, channel.id)

    async def _watch_status(self, interaction: discord.Interaction) -> str:
        """`/feed list` 末尾那行：监听服务到底接上没有、走到哪儿了。"""
        t = self.bot.t
        if not self.bot.settings.watcher_base_url:
            return t(interaction, "feed.list.watch_off")
        return t(interaction, "feed.list.watch_on", cursor=await self.cursor.get())

    async def _translator(self, channel: Any) -> Translator:
        """推送的消息没有交互对象，拿不到客户端语言 —— 用服务器配置的语言（没配就用默认）。"""
        guild = getattr(channel, "guild", None)
        locale = None
        if guild is not None:
            settings = await self.bot.guild_settings.get(guild.id)
            locale = settings.locale
        return lambda key, **kwargs: self.bot.i18n.t(locale, key, **kwargs)

    # ------------------------------------------------------------------ 内部

    def _session_or_create(self) -> aiohttp.ClientSession:
        if self._session is None or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    def _source_or_create(self) -> ReleaseSource:
        if self._source is None:
            if self._transport is None:
                self._transport = AiohttpTransport(self._session_or_create())
            self._source = GitHubReleaseSource(
                self._transport,
                token=self.bot.settings.github_token,
                logger=self.bot.log.getChild("github"),
            )
        return self._source

    def _resolve_target(self, interaction: discord.Interaction, target: FeedTarget | None) -> Any:
        """默认目标就是**运行命令的地方**：在帖子里跑就绑那个帖子，在频道里跑就绑那个频道。"""
        channel = target or interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            raise UserError("feed.bad_target")
        return channel

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild


def _normalize_repo(raw: str) -> str:
    value = raw.strip().removeprefix("https://github.com/").strip("/")
    if not REPO_PATTERN.match(value):
        raise UserError("feed.bad_repo", value=raw)
    return value


def _channel_mention(guild: discord.Guild, channel_id: int) -> str:
    channel = guild.get_channel_or_thread(channel_id)
    return channel.mention if channel is not None else f"`{channel_id}`"
