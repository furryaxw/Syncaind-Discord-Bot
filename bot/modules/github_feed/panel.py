"""`/feed` 面板：仓库订阅的列表、添加、删除与手动同步。

旧子命令方法保留为内部实现，命令树只保留 `/feed`。
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import error_embed, info_embed
from bot.modules.github_feed.cog import GitHubFeedCog, _normalize_repo

LOGGER = logging.getLogger("bot.github_feed")


class FeedPanelCog(commands.Cog):
    def __init__(self, bot: Any, legacy: GitHubFeedCog) -> None:
        self.bot = bot
        self.legacy = legacy

    def t(self, interaction: discord.Interaction, key: str, **kwargs: Any) -> str:
        return self.bot.t(interaction, key, **kwargs)

    @app_commands.command(
        name="feed",
        description=localized("GitHub release feeds for this server", "commands.feed.panel_description"),
    )
    async def feed(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        if not getattr(interaction.permissions, "manage_guild", False):
            raise UserError(
                "errors.missing_permissions",
                permissions=self.t(interaction, "permissions.manage_guild"),
            )
        targets = await self.legacy.store.all(interaction.guild_id)
        embed = info_embed(
            self.t(interaction, "feed.panel.title"),
            self.t(interaction, "feed.panel.body", count=len(targets)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=FeedPanelView(self, interaction.user.id, interaction.locale),
            ephemeral=True,
        )


class PanelView(discord.ui.View):
    def __init__(self, cog: FeedPanelCog, author_id: int, locale: Any, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.locale = locale

    def label(self, key: str) -> str:
        return self.cog.bot.i18n.t(self.locale, key)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                self.cog.bot.i18n.t(self.locale, "feed.panel.not_yours"), ephemeral=True
            )
            return False
        return True


class FeedPanelView(PanelView):
    def __init__(self, cog: FeedPanelCog, author_id: int, locale: Any) -> None:
        super().__init__(cog, author_id, locale)
        self.add_item(_button(self.label("feed.panel.list"), discord.ButtonStyle.primary, self._list))
        self.add_item(_button(self.label("feed.panel.add"), discord.ButtonStyle.success, self._add))
        self.add_item(_button(self.label("feed.panel.remove"), discord.ButtonStyle.secondary, self._remove))
        self.add_item(_button(self.label("feed.panel.sync"), discord.ButtonStyle.secondary, self._sync))

    async def _list(self, interaction: discord.Interaction) -> None:
        await self.cog.legacy.feed_list.callback(self.cog.legacy, interaction)

    async def _add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RepoModal(self.cog, self.locale, action="add"))

    async def _remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RepoModal(self.cog, self.locale, action="remove"))

    async def _sync(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RepoModal(self.cog, self.locale, action="sync"))


class RepoModal(discord.ui.Modal):
    def __init__(self, cog: FeedPanelCog, locale: Any, *, action: str) -> None:
        super().__init__(title=cog.bot.i18n.t(locale, f"feed.panel.{action}"))
        self.cog = cog
        self.locale = locale
        self.action = action
        self.repo = discord.ui.TextInput(label=cog.bot.i18n.t(locale, "feed.panel.repo"), max_length=200)
        self.add_item(self.repo)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if self.action == "remove":
            await self.cog.legacy.feed_remove.callback(
                self.cog.legacy, interaction, repo=self.repo.value
            )
            return
        if self.action == "sync":
            await self._sync(interaction)
            return
        await self._add(interaction)

    async def _sync(self, interaction: discord.Interaction) -> None:
        normalized = _normalize_repo(self.repo.value)
        target = await self.cog.legacy.store.get(interaction.guild_id, normalized)
        if target is None:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "feed.not_watched", repo=normalized)),
                ephemeral=True,
            )
            return
        channel = interaction.guild.get_channel_or_thread(target.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                embed=error_embed(
                    self.cog.t(interaction, "feed.channel_gone", channel=target.channel_id)
                ),
                ephemeral=True,
            )
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        posted = await self.cog.legacy.sync_target(target, channel)
        await interaction.followup.send(
            embed=info_embed(
                self.cog.t(interaction, "feed.sync.title"),
                self.cog.t(
                    interaction,
                    "feed.sync.done" if posted else "feed.sync.nothing",
                    repo=normalized,
                    count=posted,
                ),
            ),
            ephemeral=True,
        )

    async def _add(self, interaction: discord.Interaction) -> None:
        normalized = _normalize_repo(self.repo.value)
        channel = self.cog.legacy._resolve_target(interaction, None)
        await interaction.response.defer(ephemeral=True, thinking=True)
        releases = await self.cog.legacy._fetch(normalized)
        posted = await self.cog.legacy._post_all(channel, list(reversed(releases)))
        newest = max((release.release_id for release in releases), default=None)
        await self.cog.legacy.store.add(
            interaction.guild_id,
            repo=normalized,
            channel_id=channel.id,
            cursor_release_id=newest,
            created_by=interaction.user.id,
        )
        await interaction.followup.send(
            embed=info_embed(
                self.cog.t(interaction, "feed.add.title"),
                self.cog.t(
                    interaction,
                    "feed.add.done" if posted else "feed.add.no_releases",
                    repo=normalized,
                    channel=channel.mention,
                    count=posted,
                ),
            ),
            ephemeral=True,
        )


def _button(label: str, style: discord.ButtonStyle, callback) -> discord.ui.Button:
    button = discord.ui.Button(label=label, style=style)
    button.callback = callback
    return button


__all__ = ["FeedPanelCog"]
