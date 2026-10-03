"""moderation 的命令层：/kick /ban /unban /timeout /warn /purge。"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.audit import audit_reason, post_to_mod_log
from bot.core.checks import can_act_on
from bot.core.errors import UserError
from bot.core.store import GuildSettings
from bot.core.translator import localized
from bot.core.ui import ConfirmView, ask_confirmation, info_embed, ok_embed, warn_embed

from .service import ModerationService, RecordedAction, should_escalate

LOGGER = logging.getLogger("bot.moderation")

MAX_REASON_LENGTH = 480
MAX_TIMEOUT_MINUTES = 28 * 24 * 60
MAX_PURGE_MINUTES = 7 * 24 * 60

ApplyCallable = Callable[[], Awaitable[None]]


@app_commands.guild_only()
class ModerationCog(commands.Cog):
    """服务器管理：处罚、警告与消息清理。"""

    def __init__(self, bot: Any, service: ModerationService) -> None:
        self.bot = bot
        self.service = service

    # ------------------------------------------------------------------ 命令

    @app_commands.command(
        name="kick",
        description=localized("Kick a member from the server", "commands.kick.description"),
        extras={"permissions": ("kick_members",)},
    )
    @app_commands.describe(
        member=localized("The member to kick", "commands.kick.param_member"),
        reason=localized("Why this member is being kicked", "commands.kick.param_reason"),
    )
    @app_commands.checks.has_permissions(kick_members=True)
    async def kick(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        clean_reason = self._clean_reason(reason)
        await self._execute(
            interaction,
            action="kick",
            target=member,
            reason=clean_reason,
            duration_seconds=None,
            apply=lambda: member.kick(reason=self._audit_reason(interaction, clean_reason)),
        )

    @app_commands.command(
        name="ban",
        description=localized("Ban a member from the server", "commands.ban.description"),
        extras={"permissions": ("ban_members",)},
    )
    @app_commands.describe(
        member=localized("The member to ban", "commands.ban.param_member"),
        reason=localized("Why this member is being banned", "commands.ban.param_reason"),
        delete_message_days=localized(
            "Also delete this member's messages from the last N days (0-7)",
            "commands.ban.param_delete_message_days",
        ),
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def ban(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
        delete_message_days: app_commands.Range[int, 0, 7] | None = None,
    ) -> None:
        clean_reason = self._clean_reason(reason)
        seconds = int(delete_message_days or 0) * 86400
        await self._execute(
            interaction,
            action="ban",
            target=member,
            reason=clean_reason,
            duration_seconds=None,
            apply=lambda: member.ban(
                reason=self._audit_reason(interaction, clean_reason),
                delete_message_seconds=seconds,
            ),
            extra_lines=(
                [self.bot.t(interaction, "moderation.ban.deleted_days", days=delete_message_days)]
                if delete_message_days
                else None
            ),
        )

    @app_commands.command(
        name="unban",
        description=localized("Lift a ban by user ID", "commands.unban.description"),
        extras={"permissions": ("ban_members",)},
    )
    @app_commands.describe(
        user_id=localized("The banned user's ID", "commands.unban.param_user_id"),
        reason=localized("Why the ban is lifted", "commands.unban.param_reason"),
    )
    @app_commands.checks.has_permissions(ban_members=True)
    async def unban(
        self,
        interaction: discord.Interaction,
        user_id: str,
        reason: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        target = self._parse_user_id(interaction, user_id)
        clean_reason = self._clean_reason(reason)

        try:
            await guild.fetch_ban(target)
        except discord.NotFound as exc:
            raise UserError("moderation.unban.not_banned", user_id=user_id) from exc

        await self._execute(
            interaction,
            action="unban",
            target=target,
            reason=clean_reason,
            duration_seconds=None,
            check_hierarchy=False,
            apply=lambda: guild.unban(target, reason=self._audit_reason(interaction, clean_reason)),
        )

    @app_commands.command(
        name="timeout",
        description=localized(
            "Time out a member (Discord's native communication pause)",
            "commands.timeout.description",
        ),
        extras={"permissions": ("moderate_members",)},
    )
    @app_commands.describe(
        member=localized("The member to time out", "commands.timeout.param_member"),
        minutes=localized(
            "Duration in minutes (1 - 40320, i.e. up to 28 days)",
            "commands.timeout.param_minutes",
        ),
        reason=localized("Why this member is being timed out", "commands.timeout.param_reason"),
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def timeout(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        minutes: app_commands.Range[int, 1, MAX_TIMEOUT_MINUTES],
        reason: str | None = None,
    ) -> None:
        clean_reason = self._clean_reason(reason)
        duration = timedelta(minutes=int(minutes))
        await self._execute(
            interaction,
            action="timeout",
            target=member,
            reason=clean_reason,
            duration_seconds=int(duration.total_seconds()),
            apply=lambda: member.timeout(duration, reason=self._audit_reason(interaction, clean_reason)),
        )

    @app_commands.command(
        name="warn",
        description=localized(
            "Warn a member; warnings accumulate and can escalate automatically",
            "commands.warn.description",
        ),
        extras={"permissions": ("moderate_members",)},
    )
    @app_commands.describe(
        member=localized("The member to warn", "commands.warn.param_member"),
        reason=localized("Why this member is being warned", "commands.warn.param_reason"),
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def warn(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        reason: str | None = None,
    ) -> None:
        self._check_hierarchy(interaction, member)
        clean_reason = self._clean_reason(reason)
        guild = self._require_guild(interaction)

        await interaction.response.defer(ephemeral=True)
        record, active = await self.service.add_warning(
            guild.id,
            target_id=member.id,
            moderator_id=interaction.user.id,
            reason=clean_reason,
        )
        settings = await self.bot.guild_settings.get(guild.id)

        extra_lines = [
            self.bot.t(
                interaction,
                "moderation.warn.count",
                count=active,
                threshold=settings.warn_threshold,
            )
        ]
        if should_escalate(active_warnings=active, threshold=settings.warn_threshold):
            note = await self._escalate(interaction, member, settings, active)
            if note:
                extra_lines.append(note)

        await self._report(
            interaction,
            action="warn",
            target=member,
            reason=clean_reason,
            duration_seconds=None,
            record=record,
            extra_lines=extra_lines,
        )

    @app_commands.command(
        name="purge",
        description=localized("Bulk delete recent messages in this channel", "commands.purge.description"),
        extras={"permissions": ("manage_messages",)},
    )
    @app_commands.describe(
        count=localized("How many messages to scan and delete (1-100)", "commands.purge.param_count"),
        member=localized("Only delete messages from this member", "commands.purge.param_member"),
        minutes=localized("Only delete messages from the last N minutes", "commands.purge.param_minutes"),
    )
    @app_commands.checks.has_permissions(manage_messages=True)
    async def purge(
        self,
        interaction: discord.Interaction,
        count: app_commands.Range[int, 1, 100],
        member: discord.Member | None = None,
        minutes: app_commands.Range[int, 1, MAX_PURGE_MINUTES] | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            raise UserError("errors.not_a_text_channel")

        cutoff = discord.utils.utcnow() - timedelta(minutes=int(minutes)) if minutes is not None else None

        def matches(message: discord.Message) -> bool:
            if member is not None and message.author.id != member.id:
                return False
            return not (cutoff is not None and message.created_at < cutoff)

        view = self._confirm_view(interaction)
        embed = warn_embed(
            self.bot.t(interaction, "moderation.confirm.title"),
            self.bot.t(
                interaction,
                "moderation.confirm.description",
                target=channel.mention,
                action=self.bot.t(interaction, "actions.purge"),
                reason=self._filter_summary(interaction, member, minutes),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(
                embed=info_embed(self.bot.t(interaction, "common.aborted")), view=None
            )
            return

        deleted = await channel.purge(
            limit=int(count),
            check=matches,
            reason=self._audit_reason(interaction, None),
        )

        record = await self.service.record_action(
            guild.id,
            action="purge",
            target_id=member.id if member is not None else 0,
            moderator_id=interaction.user.id,
            reason=self._filter_summary(interaction, member, minutes),
        )
        await self._post_mod_log(
            guild,
            interaction,
            action="purge",
            target_text=channel.mention,
            reason=self._filter_summary(interaction, member, minutes),
            duration_seconds=None,
            record=record,
        )
        await interaction.edit_original_response(
            embed=ok_embed(
                self.bot.t(interaction, "moderation.done.title"),
                "\n".join(
                    [
                        self.bot.t(
                            interaction,
                            "moderation.done.description",
                            action=self.bot.t(interaction, "actions.purge"),
                            target=channel.mention,
                            case=record.case_number,
                        ),
                        self.bot.t(interaction, "moderation.purge.deleted", count=len(deleted)),
                    ]
                ),
            ),
            view=None,
        )

    # ------------------------------------------------------------------ 公共流程

    async def _execute(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        target: discord.Member | discord.User,
        reason: str | None,
        duration_seconds: int | None,
        apply: ApplyCallable,
        check_hierarchy: bool = True,
        extra_lines: list[str] | None = None,
    ) -> None:
        """危险操作的标准流程：层级校验 → 确认 → 执行 → 落库 → DM → mod-log。"""
        if check_hierarchy:
            self._check_hierarchy(interaction, target)

        view = self._confirm_view(interaction)
        embed = warn_embed(
            self.bot.t(interaction, "moderation.confirm.title"),
            self.bot.t(
                interaction,
                "moderation.confirm.description",
                target=self._target_text(target),
                action=self.bot.t(interaction, f"actions.{action}"),
                reason=reason or self.bot.t(interaction, "common.no_reason"),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(
                embed=info_embed(self.bot.t(interaction, "common.aborted")), view=None
            )
            return

        await apply()

        guild = self._require_guild(interaction)
        record = await self.service.record_action(
            guild.id,
            action=action,
            target_id=target.id,
            moderator_id=interaction.user.id,
            reason=reason,
            duration_seconds=duration_seconds,
        )
        await self._report(
            interaction,
            action=action,
            target=target,
            reason=reason,
            duration_seconds=duration_seconds,
            record=record,
            extra_lines=extra_lines,
        )

    async def _report(
        self,
        interaction: discord.Interaction,
        *,
        action: str,
        target: discord.Member | discord.User,
        reason: str | None,
        duration_seconds: int | None,
        record: RecordedAction,
        extra_lines: list[str] | None = None,
    ) -> None:
        """落库之后的事：DM 当事人、发 mod-log、回复执行者。"""
        guild = self._require_guild(interaction)
        await self._notify_target(guild, target, action, reason, duration_seconds)
        await self._post_mod_log(
            guild,
            interaction,
            action=action,
            target_text=self._target_text(target),
            reason=reason,
            duration_seconds=duration_seconds,
            record=record,
        )

        lines = [
            self.bot.t(
                interaction,
                "moderation.done.description",
                action=self.bot.t(interaction, f"actions.{action}"),
                target=self._target_text(target),
                case=record.case_number,
            )
        ]
        if duration_seconds:
            lines.append(
                self.bot.t(
                    interaction,
                    "moderation.done.duration",
                    minutes=duration_seconds // 60,
                )
            )
        lines.extend(extra_lines or [])

        await interaction.edit_original_response(
            embed=ok_embed(self.bot.t(interaction, "moderation.done.title"), "\n".join(lines)),
            view=None,
        )

    async def _escalate(
        self,
        interaction: discord.Interaction,
        member: discord.Member,
        settings: GuildSettings,
        active_warnings: int,
    ) -> str | None:
        """按服务器的 warn_action 设置自动升级处罚，并单独留一条 case。"""
        guild = self._require_guild(interaction)
        action = settings.warn_action
        reason = self.bot.t(
            interaction,
            "moderation.escalation.reason",
            count=active_warnings,
            threshold=settings.warn_threshold,
        )
        duration_seconds: int | None = None

        try:
            if action == "timeout":
                duration_seconds = settings.warn_timeout_minutes * 60
                await member.timeout(timedelta(seconds=duration_seconds), reason=reason)
            elif action == "kick":
                await member.kick(reason=reason)
            elif action == "ban":
                await member.ban(reason=reason, delete_message_seconds=0)
            else:
                LOGGER.error("未知的 warn_action：%s", action)
                return None
        except discord.HTTPException:
            LOGGER.exception("自动升级处罚失败：guild=%s target=%s action=%s", guild.id, member.id, action)
            return self.bot.t(interaction, "moderation.escalation.failed")

        record = await self.service.record_action(
            guild.id,
            action=f"auto_{action}",
            target_id=member.id,
            moderator_id=interaction.client.user.id if interaction.client.user else 0,
            reason=reason,
            duration_seconds=duration_seconds,
            automated=True,
        )
        await self._notify_target(guild, member, f"auto_{action}", reason, duration_seconds)
        await self._post_mod_log(
            guild,
            interaction,
            action=f"auto_{action}",
            target_text=self._target_text(member),
            reason=reason,
            duration_seconds=duration_seconds,
            record=record,
        )
        return self.bot.t(
            interaction,
            "moderation.escalation.applied",
            action=self.bot.t(interaction, f"actions.auto_{action}"),
            case=record.case_number,
        )

    # ------------------------------------------------------------------ 小工具

    def _check_hierarchy(self, interaction: discord.Interaction, target: Any) -> None:
        guild = self._require_guild(interaction)
        actor = guild.get_member(interaction.user.id)
        denial = can_act_on(
            actor=actor,
            target=target,
            bot_member=guild.me,
            guild_owner_id=guild.owner_id,
        )
        if denial:
            raise UserError(denial, target=self._target_text(target))

    def _confirm_view(self, interaction: discord.Interaction) -> ConfirmView:
        return ConfirmView(
            author_id=interaction.user.id,
            confirm_label=self.bot.t(interaction, "common.confirm"),
            cancel_label=self.bot.t(interaction, "common.cancel"),
            not_author_message=self.bot.t(interaction, "common.not_your_button"),
        )

    def _clean_reason(self, reason: str | None) -> str | None:
        if reason is None:
            return None
        cleaned = reason.strip()
        if not cleaned:
            return None
        return cleaned[:MAX_REASON_LENGTH]

    def _audit_reason(self, interaction: discord.Interaction, reason: str | None) -> str:
        return audit_reason(interaction, reason)

    def _filter_summary(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None,
        minutes: int | None,
    ) -> str:
        parts = []
        if member is not None:
            parts.append(self.bot.t(interaction, "moderation.purge.filter_member", member=self._target_text(member)))
        if minutes is not None:
            parts.append(self.bot.t(interaction, "moderation.purge.filter_minutes", minutes=minutes))
        return " · ".join(parts) if parts else self.bot.t(interaction, "moderation.purge.filter_all")

    def _parse_user_id(self, interaction: discord.Interaction, raw: str) -> discord.Object:
        text = raw.strip().strip("<@!>")
        if not text.isdigit():
            raise UserError("moderation.invalid_user_id", value=raw)
        return discord.Object(id=int(text))

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild

    @staticmethod
    def _target_text(target: Any) -> str:
        mention = getattr(target, "mention", None)
        if mention:
            return f"{mention} (`{target.id}`)"
        return f"`{target.id}`"

    async def _notify_target(
        self,
        guild: discord.Guild,
        target: Any,
        action: str,
        reason: str | None,
        duration_seconds: int | None,
    ) -> None:
        """尽力 DM 当事人。对方关了私信就静默跳过——这不是处罚失败。

        注意：DM 里拿不到对方的客户端语言（``locale`` 只存在于交互对象上），
        所以这里统一用 i18n 的默认语言。
        """
        if not isinstance(target, discord.abc.User) or target.bot:
            return
        t = self.bot.i18n.t
        duration = t(None, "moderation.dm.duration", minutes=duration_seconds // 60) if duration_seconds else ""
        embed = info_embed(
            t(None, "moderation.dm.title"),
            t(
                None,
                "moderation.dm.description",
                guild=guild.name,
                action=t(None, f"actions.{action}"),
                reason=reason or t(None, "common.no_reason"),
                duration=duration,
            ),
        )
        try:
            await target.send(embed=embed)
        except discord.HTTPException:
            LOGGER.debug("无法 DM 当事人 %s（可能关闭了私信）", target.id)

    async def _post_mod_log(
        self,
        guild: discord.Guild,
        interaction: discord.Interaction,
        *,
        action: str,
        target_text: str,
        reason: str | None,
        duration_seconds: int | None,
        record: RecordedAction,
    ) -> None:
        t = self.bot.t
        embed = info_embed(t(interaction, "moderation.log.title"))
        embed.add_field(name=t(interaction, "moderation.log.case"), value=f"#{record.case_number}", inline=True)
        embed.add_field(
            name=t(interaction, "moderation.log.action"),
            value=t(interaction, f"actions.{action}"),
            inline=True,
        )
        embed.add_field(
            name=t(interaction, "moderation.log.target"),
            value=target_text,
            inline=True,
        )
        embed.add_field(
            name=t(interaction, "moderation.log.moderator"),
            value=f"{interaction.user.mention} (`{interaction.user.id}`)",
            inline=True,
        )
        if duration_seconds:
            embed.add_field(
                name=t(interaction, "moderation.log.duration"),
                value=t(interaction, "moderation.duration.minutes", minutes=duration_seconds // 60),
                inline=True,
            )
        embed.add_field(
            name=t(interaction, "moderation.log.reason"),
            value=reason or t(interaction, "common.no_reason"),
            inline=False,
        )
        embed.set_footer(text=t(interaction, "moderation.log.time", time=record.created_at))

        await post_to_mod_log(self.bot, guild, embed)
