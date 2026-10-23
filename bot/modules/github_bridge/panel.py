"""`/github` 面板：账号绑定、全部绑定与反查。

旧的 `/link` 命令暂时保留给已有测试与过渡；新入口统一走这里。
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import info_embed, send_paged_lines, warn_embed
from bot.integrations.github import GitHubAccountStore

from .cog import GitHubBridgeCog

LOGGER = logging.getLogger("bot.github")


class GitHubPanelCog(commands.Cog):
    def __init__(self, bot: Any, bridge: GitHubBridgeCog, store: GitHubAccountStore) -> None:
        self.bot = bot
        self.bridge = bridge
        self.store = store

    def t(self, interaction: discord.Interaction, key: str, **kwargs: Any) -> str:
        return self.bot.t(interaction, key, **kwargs)

    def _is_admin(self, interaction: discord.Interaction) -> bool:
        return bool(getattr(interaction.permissions, "manage_roles", False))

    @app_commands.command(
        name="github",
        description=localized("GitHub account binding and lookup", "commands.github.description"),
    )
    @app_commands.describe(
        get_member=localized("Whose GitHub binding to show", "commands.github.param_member"),
        get_group=localized("List bindings of members with this role", "commands.github.param_group"),
        check=localized("Reverse lookup by GitHub login or numeric ID", "commands.github.param_check"),
    )
    async def github(
        self,
        interaction: discord.Interaction,
        get_member: discord.Member | None = None,
        get_group: discord.Role | None = None,
        check: str | None = None,
    ) -> None:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        supplied = [value for value in (get_member, get_group, check) if value is not None]
        if len(supplied) > 1:
            raise UserError("github.one_param")
        if get_member is not None:
            await self._show_member(interaction, get_member)
            return
        if get_group is not None:
            await self._show_group(interaction, get_group)
            return
        if check is not None:
            await self._show_check(interaction, check)
            return
        await self._show_panel(interaction, edit=False)

    # ------------------------------------------------------------------ 面板

    async def _show_panel(self, interaction: discord.Interaction, *, edit: bool) -> None:
        account = await self.store.get(interaction.guild_id, interaction.user.id)
        if account is None:
            body = self.t(interaction, "github.panel.unlinked")
        else:
            body = self.t(
                interaction,
                "github.panel.linked",
                login=account.github_login,
                github_id=account.github_user_id,
                time=account.linked_at,
            )
        embed = info_embed(self.t(interaction, "github.panel.title"), body)
        view = GitHubPanelView(
            self,
            interaction.user.id,
            interaction.locale,
            linked=account is not None,
            admin=self._is_admin(interaction),
        )
        if edit:
            await interaction.response.edit_message(embed=embed, view=view)
        else:
            await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

    async def _show_member(self, interaction: discord.Interaction, member: discord.Member) -> None:
        if member.id != interaction.user.id and not self._is_admin(interaction):
            raise UserError("errors.missing_permissions", permissions=self.t(interaction, "permissions.manage_roles"))
        account = await self.store.get(interaction.guild_id, member.id)
        if account is None:
            await interaction.response.send_message(
                embed=info_embed(
                    self.t(interaction, "github.member.title", member=member.display_name),
                    self.t(interaction, "github.show.empty"),
                ),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=info_embed(
                self.t(interaction, "github.member.title", member=member.display_name),
                self.t(
                    interaction,
                    "github.show.value",
                    login=account.github_login,
                    id=account.github_user_id,
                    time=account.linked_at,
                ),
            ),
            ephemeral=True,
        )

    async def _show_group(self, interaction: discord.Interaction, role: discord.Role) -> None:
        if not self._is_admin(interaction):
            raise UserError("errors.missing_permissions", permissions=self.t(interaction, "permissions.manage_roles"))
        accounts = {
            account.discord_user_id: account for account in await self.store.all(interaction.guild_id)
        }
        lines = [
            self.t(
                interaction,
                "github.group.line",
                member=f"<@{member.id}>",
                login=account.github_login,
                github_id=account.github_user_id,
            )
            for member in role.members
            if (account := accounts.get(member.id)) is not None
        ]
        if not lines:
            await interaction.response.send_message(
                embed=info_embed(
                    self.t(interaction, "github.group.title", role=role.name),
                    self.t(interaction, "github.group.none"),
                ),
                ephemeral=True,
            )
            return
        await send_paged_lines(
            interaction,
            title=self.t(interaction, "github.group.title", role=role.name),
            lines=lines,
            translate=lambda key, **kw: self.t(interaction, key, **kw),
        )

    async def _show_check(self, interaction: discord.Interaction, query: str) -> None:
        if not self._is_admin(interaction):
            raise UserError("errors.missing_permissions", permissions=self.t(interaction, "permissions.manage_roles"))
        account = await self._lookup(interaction.guild_id, query)
        if account is None:
            await interaction.response.send_message(
                embed=info_embed(self.t(interaction, "github.check.title"), self.t(interaction, "github.check.not_found")),
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            embed=info_embed(
                self.t(interaction, "github.check.title"),
                self.t(
                    interaction,
                    "github.check.found",
                    member=f"<@{account.discord_user_id}>",
                    login=account.github_login,
                    github_id=account.github_user_id,
                    time=account.linked_at,
                ),
            ),
            ephemeral=True,
        )

    async def _lookup(self, guild_id: int, query: str):
        value = query.strip()
        if not value:
            return None
        if value.isdigit():
            return await self.store.by_github_id(guild_id, int(value))
        return await self.store.by_github_login(guild_id, value)

    async def _show_all(self, interaction: discord.Interaction) -> None:
        if not self._is_admin(interaction):
            raise UserError("errors.missing_permissions", permissions=self.t(interaction, "permissions.manage_roles"))
        accounts = await self.store.all(interaction.guild_id)
        if not accounts:
            await interaction.response.send_message(
                embed=info_embed(self.t(interaction, "github.all.title"), self.t(interaction, "github.all.none")),
                ephemeral=True,
            )
            return
        lines = [
            self.t(
                interaction,
                "github.all.line",
                member=f"<@{account.discord_user_id}>",
                login=account.github_login,
                github_id=account.github_user_id,
                time=account.linked_at,
            )
            for account in accounts
        ]
        await send_paged_lines(
            interaction,
            title=self.t(interaction, "github.all.title"),
            lines=lines,
            translate=lambda key, **kw: self.t(interaction, key, **kw),
        )


class GitHubPanelView(discord.ui.View):
    def __init__(
        self, cog: GitHubPanelCog, author_id: int, locale: Any, *, linked: bool, admin: bool
    ) -> None:
        super().__init__(timeout=300)
        self.cog = cog
        self.author_id = author_id
        self.locale = locale
        if not linked:
            bind = discord.ui.Button(label=cog.bot.i18n.t(locale, "github.panel.bind"), style=discord.ButtonStyle.success)
            bind.callback = self._bind
            self.add_item(bind)
        else:
            unbind = discord.ui.Button(label=cog.bot.i18n.t(locale, "github.panel.unbind"), style=discord.ButtonStyle.danger)
            unbind.callback = self._unbind
            self.add_item(unbind)
        if admin:
            all_button = discord.ui.Button(
                label=cog.bot.i18n.t(locale, "github.panel.all"), style=discord.ButtonStyle.secondary
            )
            check_button = discord.ui.Button(
                label=cog.bot.i18n.t(locale, "github.panel.check"), style=discord.ButtonStyle.secondary
            )
            all_button.callback = self._all
            check_button.callback = self._check
            self.add_item(all_button)
            self.add_item(check_button)
        close = discord.ui.Button(label=cog.bot.i18n.t(locale, "github.panel.close"), style=discord.ButtonStyle.secondary)
        close.callback = self._close
        self.add_item(close)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                self.cog.bot.i18n.t(self.locale, "github.panel.not_yours"), ephemeral=True
            )
            return False
        return True

    async def _bind(self, interaction: discord.Interaction) -> None:
        await self.cog.bridge.start_binding(interaction)

    async def _unbind(self, interaction: discord.Interaction) -> None:
        view = ConfirmUnbindView(self.cog, interaction.user.id, self.locale)
        await interaction.response.edit_message(
            embed=warn_embed(self.cog.bot.i18n.t(self.locale, "github.panel.unbind_confirm")),
            view=view,
        )

    async def _all(self, interaction: discord.Interaction) -> None:
        await self.cog._show_all(interaction)

    async def _check(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CheckModal(self.cog, self.locale))

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)


class ConfirmUnbindView(discord.ui.View):
    def __init__(self, cog: GitHubPanelCog, author_id: int, locale: Any) -> None:
        super().__init__(timeout=60)
        self.cog = cog
        self.author_id = author_id
        self.locale = locale
        confirm = discord.ui.Button(
            label=cog.bot.i18n.t(locale, "github.panel.unbind"),
            style=discord.ButtonStyle.danger,
        )
        cancel = discord.ui.Button(
            label=cog.bot.i18n.t(locale, "github.panel.cancel"),
            style=discord.ButtonStyle.secondary,
        )
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.cog.store.unlink(interaction.guild_id, interaction.user.id)
        await self.cog._show_panel(interaction, edit=True)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await self.cog._show_panel(interaction, edit=True)


class CheckModal(discord.ui.Modal):
    def __init__(self, cog: GitHubPanelCog, locale: Any) -> None:
        super().__init__(title=cog.bot.i18n.t(locale, "github.check.modal_title"))
        self.cog = cog
        self.input = discord.ui.TextInput(
            label=cog.bot.i18n.t(locale, "github.check.modal_label"),
            max_length=100,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog._show_check(interaction, self.input.value)


__all__ = ["GitHubPanelCog"]
