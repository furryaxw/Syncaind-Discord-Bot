"""channels 的命令层：``/channel`` 一组。

参数一律用 discord.py 的转换器（``discord.TextChannel`` / ``discord.CategoryChannel`` /
``discord.Role | discord.Member``）来解析，处理器自己不做 ``isinstance`` 判断——
类型校验交给库，这样处理器逻辑保持可测。
"""

from __future__ import annotations

import logging
from typing import Any, Literal

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.audit import audit_reason
from bot.core.errors import UserError
from bot.core.permission_flags import (
    PermissionParseError,
    build_overwrite,
    inherit_overwrite,
    parse_flags,
)
from bot.core.translator import localized
from bot.core.ui import ConfirmView, add_fields, ask_confirmation, info_embed, ok_embed, warn_embed

LOGGER = logging.getLogger("bot.channels")

ChannelKind = Literal["text", "voice", "category"]

MAX_SLOWMODE_SECONDS = 21600  # Discord 的上限是 6 小时


@app_commands.guild_only()
class ChannelsCog(commands.Cog):
    """频道管理。"""

    channel_group = app_commands.Group(
        name="channel",
        description=localized("Create, edit and lock channels", "commands.channel.description"),
    )

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ 建 / 删 / 改名

    @channel_group.command(
        name="create",
        description=localized("Create a channel", "commands.channel.create.description"),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(
        name=localized("Channel name", "commands.channel.create.param_name"),
        kind=localized("What to create", "commands.channel.create.param_kind"),
        category=localized("Category to put it in", "commands.channel.create.param_category"),
        topic=localized("Channel topic (text channels only)", "commands.channel.create.param_topic"),
        private=localized("Hide it from @everyone", "commands.channel.create.param_private"),
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        kind: ChannelKind = "text",
        category: discord.CategoryChannel | None = None,
        topic: str | None = None,
        private: bool = False,
    ) -> None:
        guild = self._require_guild(interaction)
        reason = audit_reason(interaction)
        overwrites = {guild.default_role: discord.PermissionOverwrite(view_channel=False)} if private else None

        if kind == "category":
            channel = await guild.create_category(name, overwrites=overwrites, reason=reason)
        elif kind == "voice":
            channel = await guild.create_voice_channel(name, category=category, overwrites=overwrites, reason=reason)
        else:
            channel = await guild.create_text_channel(
                name, category=category, topic=topic, overwrites=overwrites, reason=reason
            )

        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "channels.create.done_title"),
                self.bot.t(interaction, "channels.create.done", channel=self._mentions(channel)),
            ),
            ephemeral=True,
        )

    @channel_group.command(
        name="delete",
        description=localized("Delete a channel", "commands.channel.delete.description"),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(
        channel=localized("The channel to delete", "commands.channel.delete.param_channel"),
        reason=localized("Why it is being deleted", "commands.channel.delete.param_reason"),
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def delete(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        reason: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        t = self.bot.t
        clean_reason = self._clean_reason(reason)

        view = self._confirm_view(interaction)
        embed = warn_embed(
            t(interaction, "channels.confirm.title"),
            t(
                interaction,
                "channels.confirm.delete",
                target=self._mentions(channel),
                reason=clean_reason or t(interaction, "common.no_reason"),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(embed=info_embed(t(interaction, "common.aborted")), view=None)
            return

        await channel.delete(reason=audit_reason(interaction, clean_reason))
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "channels.delete.done_title"),
                t(interaction, "channels.delete.done", channel=self._mentions(channel)),
            ),
            view=None,
        )
        LOGGER.info("已删除频道 %s（guild=%s）", channel.id, guild.id)

    @channel_group.command(
        name="rename",
        description=localized("Rename a channel", "commands.channel.rename.description"),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(
        channel=localized("The channel to rename", "commands.channel.rename.param_channel"),
        name=localized("The new name", "commands.channel.rename.param_name"),
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def rename(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        name: str,
    ) -> None:
        await channel.edit(name=name, reason=audit_reason(interaction))
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "channels.rename.done_title"),
                self.bot.t(interaction, "channels.rename.done", channel=self._mentions(channel), name=name),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 锁定 / 慢速

    @channel_group.command(
        name="lock",
        description=localized("Stop @everyone from sending messages here", "commands.channel.lock.description"),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(channel=localized("The channel to lock", "commands.channel.lock.param_channel"))
    @app_commands.checks.has_permissions(manage_channels=True)
    async def lock(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        guild = self._require_guild(interaction)
        await channel.set_permissions(
            guild.default_role,
            send_messages=False,
            send_messages_in_threads=False,
            reason=audit_reason(interaction),
        )
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "channels.lock.done_title"),
                self.bot.t(interaction, "channels.lock.done", channel=self._mentions(channel)),
            ),
            ephemeral=True,
        )

    @channel_group.command(
        name="unlock",
        description=localized("Remove that restriction again", "commands.channel.unlock.description"),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(channel=localized("The channel to unlock", "commands.channel.unlock.param_channel"))
    @app_commands.checks.has_permissions(manage_channels=True)
    async def unlock(self, interaction: discord.Interaction, channel: discord.TextChannel) -> None:
        guild = self._require_guild(interaction)
        # 置 None 而不是 True：这是「撤掉锁定」，不是「强制允许」——上层规则继续生效。
        await channel.set_permissions(
            guild.default_role,
            overwrite=inherit_overwrite("send_messages", "send_messages_in_threads"),
            reason=audit_reason(interaction),
        )
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "channels.unlock.done_title"),
                self.bot.t(interaction, "channels.unlock.done", channel=self._mentions(channel)),
            ),
            ephemeral=True,
        )

    @channel_group.command(
        name="slowmode",
        description=localized("Set the slowmode delay", "commands.channel.slowmode.description"),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(
        channel=localized("The channel to change", "commands.channel.slowmode.param_channel"),
        seconds=localized("Delay in seconds (0 disables it)", "commands.channel.slowmode.param_seconds"),
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def slowmode(
        self,
        interaction: discord.Interaction,
        channel: discord.TextChannel,
        seconds: app_commands.Range[int, 0, MAX_SLOWMODE_SECONDS],
    ) -> None:
        await channel.edit(slowmode_delay=int(seconds), reason=audit_reason(interaction))
        t = self.bot.t
        await interaction.response.send_message(
            embed=ok_embed(
                t(interaction, "channels.slowmode.done_title"),
                t(
                    interaction,
                    "channels.slowmode.done" if seconds else "channels.slowmode.cleared",
                    channel=self._mentions(channel),
                    seconds=seconds,
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 权限覆盖

    @channel_group.command(
        name="overwrite",
        description=localized(
            "Set a permission overwrite on one channel",
            "commands.channel.overwrite.description",
        ),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(
        channel=localized("The channel to change", "commands.channel.overwrite.param_channel"),
        target=localized("The role or member to grant/deny for", "commands.channel.overwrite.param_target"),
        allow=localized("Permissions to allow, separated by commas", "commands.channel.overwrite.param_allow"),
        deny=localized("Permissions to deny, separated by commas", "commands.channel.overwrite.param_deny"),
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def overwrite(
        self,
        interaction: discord.Interaction,
        channel: discord.abc.GuildChannel,
        target: discord.Role | discord.Member,
        allow: str | None = None,
        deny: str | None = None,
    ) -> None:
        overwrite = self._parse_overwrite(interaction, allow, deny)

        t = self.bot.t
        view = self._confirm_view(interaction)
        embed = warn_embed(
            t(interaction, "channels.confirm.title"),
            t(
                interaction,
                "channels.confirm.overwrite",
                channel=self._mentions(channel),
                target=self._target_text(target),
                allow=self._flags_text(interaction, allow),
                deny=self._flags_text(interaction, deny),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(embed=info_embed(t(interaction, "common.aborted")), view=None)
            return

        await channel.set_permissions(target, overwrite=overwrite, reason=audit_reason(interaction))
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "channels.overwrite.done_title"),
                t(
                    interaction,
                    "channels.overwrite.done",
                    target=self._target_text(target),
                    channel=self._mentions(channel),
                ),
            ),
            view=None,
        )

    @channel_group.command(
        name="overwrite-category",
        description=localized(
            "Apply the same overwrite to every channel in a category",
            "commands.channel.overwrite_category.description",
        ),
        extras={"permissions": ("manage_channels",)},
    )
    @app_commands.describe(
        category=localized(
            "The category whose channels are changed", "commands.channel.overwrite_category.param_category"
        ),
        target=localized("The role or member to grant/deny for", "commands.channel.overwrite_category.param_target"),
        allow=localized("Permissions to allow, separated by commas", "commands.channel.overwrite_category.param_allow"),
        deny=localized("Permissions to deny, separated by commas", "commands.channel.overwrite_category.param_deny"),
    )
    @app_commands.checks.has_permissions(manage_channels=True)
    async def overwrite_category(
        self,
        interaction: discord.Interaction,
        category: discord.CategoryChannel,
        target: discord.Role | discord.Member,
        allow: str | None = None,
        deny: str | None = None,
    ) -> None:
        overwrite = self._parse_overwrite(interaction, allow, deny)
        channels = list(category.channels)

        t = self.bot.t
        view = self._confirm_view(interaction)
        embed = warn_embed(
            t(interaction, "channels.confirm.title"),
            t(
                interaction,
                "channels.confirm.overwrite_category",
                category=category.name,
                count=len(channels),
                target=self._target_text(target),
                allow=self._flags_text(interaction, allow),
                deny=self._flags_text(interaction, deny),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(embed=info_embed(t(interaction, "common.aborted")), view=None)
            return

        reason = audit_reason(interaction)
        applied: list[str] = []
        failed: list[str] = []
        for child in channels:
            try:
                await child.set_permissions(target, overwrite=overwrite, reason=reason)
                applied.append(child.name)
            except discord.HTTPException:
                LOGGER.exception("对频道 %s 设置权限覆盖失败", child.id)
                failed.append(child.name)

        result = ok_embed(
            t(interaction, "channels.overwrite_category.done_title"),
            t(interaction, "channels.overwrite_category.done", count=len(applied), category=category.name),
        )
        if failed:
            result = add_fields(
                result,
                [(t(interaction, "channels.overwrite_category.failed"), ", ".join(failed), False)],
            )
        await interaction.edit_original_response(embed=result, view=None)

    # ------------------------------------------------------------------ 内部

    def _parse_overwrite(
        self, interaction: discord.Interaction, allow: str | None, deny: str | None
    ) -> discord.PermissionOverwrite:
        try:
            allow_flags = parse_flags(allow)
            deny_flags = parse_flags(deny)
        except PermissionParseError as exc:
            raise UserError(
                "errors.unknown_permissions",
                permissions=", ".join(f"`{name}`" for name in exc.unknown),
                valid=self.bot.t(interaction, "errors.valid_permissions_hint"),
            ) from exc
        if not allow_flags and not deny_flags:
            raise UserError("errors.no_permissions_given")
        return build_overwrite(allow_flags, deny_flags)

    def _flags_text(self, interaction: discord.Interaction, text: str | None) -> str:
        if not text or not text.strip():
            return self.bot.t(interaction, "common.none")
        try:
            flags = parse_flags(text)
        except PermissionParseError:
            return text
        return ", ".join(f"`{flag}`" for flag in flags) if flags else self.bot.t(interaction, "common.none")

    def _confirm_view(self, interaction: discord.Interaction) -> ConfirmView:
        t = self.bot.t
        return ConfirmView(
            author_id=interaction.user.id,
            confirm_label=t(interaction, "common.confirm"),
            cancel_label=t(interaction, "common.cancel"),
            not_author_message=t(interaction, "common.not_your_button"),
        )

    @staticmethod
    def _mentions(channel: Any) -> str:
        return getattr(channel, "mention", f"`{getattr(channel, 'id', '?')}`")

    @staticmethod
    def _target_text(target: Any) -> str:
        mention = getattr(target, "mention", None)
        return f"{mention} (`{target.id}`)" if mention else f"`{getattr(target, 'id', '?')}`"

    @staticmethod
    def _clean_reason(reason: str | None) -> str | None:
        if reason is None:
            return None
        cleaned = reason.strip()
        return cleaned or None

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild
