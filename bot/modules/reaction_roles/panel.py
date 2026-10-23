"""`/reactionrole` 面板：列表、添加、删除、清空。

旧的子命令方法保留为内部实现，命令树只保留 `/reactionrole`。
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
from bot.modules.reaction_roles.cog import ReactionRolesCog

LOGGER = logging.getLogger("bot.reaction_roles")


class ReactionRolePanelCog(commands.Cog):
    def __init__(self, bot: Any, legacy: ReactionRolesCog) -> None:
        self.bot = bot
        self.legacy = legacy

    def t(self, interaction: discord.Interaction, key: str, **kwargs: Any) -> str:
        return self.bot.t(interaction, key, **kwargs)

    def _admin(self, interaction: discord.Interaction) -> bool:
        return bool(getattr(interaction.permissions, "manage_roles", False))

    @app_commands.command(
        name="reactionrole",
        description=localized("Roles members can pick by reacting", "commands.reactionrole.panel_description"),
    )
    async def reactionrole(self, interaction: discord.Interaction) -> None:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        if not self._admin(interaction):
            raise UserError(
                "errors.missing_permissions",
                permissions=self.t(interaction, "permissions.manage_roles"),
            )
        mappings = await self.legacy.service.for_guild(interaction.guild_id)
        embed = info_embed(
            self.t(interaction, "reaction_roles.panel.title"),
            self.t(interaction, "reaction_roles.panel.body", count=len(mappings)),
        )
        await interaction.response.send_message(
            embed=embed,
            view=ReactionRolePanelView(self, interaction.user.id, interaction.locale),
            ephemeral=True,
        )


class PanelView(discord.ui.View):
    def __init__(self, cog: ReactionRolePanelCog, author_id: int, locale: Any, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.locale = locale

    def label(self, key: str) -> str:
        return self.cog.bot.i18n.t(self.locale, key)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                self.cog.bot.i18n.t(self.locale, "reaction_roles.panel.not_yours"), ephemeral=True
            )
            return False
        return True


class ReactionRolePanelView(PanelView):
    def __init__(self, cog: ReactionRolePanelCog, author_id: int, locale: Any) -> None:
        super().__init__(cog, author_id, locale)
        self.add_item(_button(self.label("reaction_roles.panel.list"), discord.ButtonStyle.primary, self._list))
        self.add_item(_button(self.label("reaction_roles.panel.add"), discord.ButtonStyle.success, self._add))
        self.add_item(_button(self.label("reaction_roles.panel.remove"), discord.ButtonStyle.secondary, self._remove))
        self.add_item(_button(self.label("reaction_roles.panel.clear"), discord.ButtonStyle.danger, self._clear))

    async def _list(self, interaction: discord.Interaction) -> None:
        await self.cog.legacy.list_roles.callback(self.cog.legacy, interaction)

    async def _add(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(AddModal(self.cog, self.locale))

    async def _remove(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RemoveModal(self.cog, self.locale))

    async def _clear(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(ClearModal(self.cog, self.locale))


class AddModal(discord.ui.Modal):
    def __init__(self, cog: ReactionRolePanelCog, locale: Any) -> None:
        super().__init__(title=cog.bot.i18n.t(locale, "reaction_roles.panel.add"))
        self.cog = cog
        self.locale = locale
        self.message_id = discord.ui.TextInput(
            label=cog.bot.i18n.t(locale, "reaction_roles.panel.message_id"), max_length=40
        )
        self.emoji = discord.ui.TextInput(
            label=cog.bot.i18n.t(locale, "reaction_roles.panel.emoji"), max_length=100
        )
        self.add_item(self.message_id)
        self.add_item(self.emoji)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        roles = [
            role
            for role in interaction.guild.roles
            if not role.is_default() and not role.managed and role < interaction.guild.me.top_role
        ][:25]
        if not roles:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "reaction_roles.panel.no_roles")),
                ephemeral=True,
            )
            return
        options = [
            discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in roles
        ]
        await interaction.response.send_message(
            embed=info_embed(self.cog.t(interaction, "reaction_roles.panel.pick_role")),
            view=RolePickerView(
                self.cog,
                interaction.user.id,
                self.locale,
                options,
                message_id=self.message_id.value,
                emoji=self.emoji.value,
            ),
            ephemeral=True,
        )


class RolePickerView(PanelView):
    def __init__(
        self,
        cog: ReactionRolePanelCog,
        author_id: int,
        locale: Any,
        options: list[discord.SelectOption],
        *,
        message_id: str,
        emoji: str,
    ) -> None:
        super().__init__(cog, author_id, locale)
        self.message_id = message_id
        self.emoji = emoji
        select = discord.ui.Select(placeholder=self.label("reaction_roles.panel.pick_role"), options=options)
        select.callback = self._picked
        self.add_item(select)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        role = interaction.guild.get_role(int(values[0]))
        if role is None:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "reaction_roles.panel.role_gone")),
                ephemeral=True,
            )
            return
        await self.cog.legacy.add.callback(
            self.cog.legacy,
            interaction,
            message_id=self.message_id,
            emoji=self.emoji,
            role=role,
            channel=interaction.channel,
        )


class RemoveModal(discord.ui.Modal):
    def __init__(self, cog: ReactionRolePanelCog, locale: Any) -> None:
        super().__init__(title=cog.bot.i18n.t(locale, "reaction_roles.panel.remove"))
        self.cog = cog
        self.locale = locale
        self.message_id = discord.ui.TextInput(
            label=cog.bot.i18n.t(locale, "reaction_roles.panel.message_id"), max_length=40
        )
        self.emoji = discord.ui.TextInput(
            label=cog.bot.i18n.t(locale, "reaction_roles.panel.emoji"), max_length=100
        )
        self.add_item(self.message_id)
        self.add_item(self.emoji)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.legacy.remove.callback(
            self.cog.legacy,
            interaction,
            message_id=self.message_id.value,
            emoji=self.emoji.value,
            channel=interaction.channel,
        )


class ClearModal(discord.ui.Modal):
    def __init__(self, cog: ReactionRolePanelCog, locale: Any) -> None:
        super().__init__(title=cog.bot.i18n.t(locale, "reaction_roles.panel.clear"))
        self.cog = cog
        self.locale = locale
        self.message_id = discord.ui.TextInput(
            label=cog.bot.i18n.t(locale, "reaction_roles.panel.message_id"), max_length=40
        )
        self.add_item(self.message_id)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        await self.cog.legacy.clear.callback(
            self.cog.legacy,
            interaction,
            message_id=self.message_id.value,
            channel=interaction.channel,
        )


def _button(label: str, style: discord.ButtonStyle, callback) -> discord.ui.Button:
    button = discord.ui.Button(label=label, style=style)
    button.callback = callback
    return button


__all__ = ["ReactionRolePanelCog"]
