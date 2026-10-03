"""cases 的命令层：``/history``、``/case view``、``/case revoke``。"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.audit import audit_reason, post_to_mod_log
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import ConfirmView, add_fields, ask_confirmation, info_embed, ok_embed, warn_embed

from .service import (
    DEFAULT_HISTORY_LIMIT,
    MAX_HISTORY_LIMIT,
    CaseAlreadyRevoked,
    CaseRecord,
    CaseService,
    required_permission,
    undo_plan,
)

LOGGER = logging.getLogger("bot.cases")

MAX_REASON_LENGTH = 480


@app_commands.guild_only()
class CasesCog(commands.Cog):
    """查看与撤销处罚记录。"""

    case_group = app_commands.Group(
        name="case",
        description=localized("Inspect or revoke one moderation case", "commands.case.description"),
    )

    def __init__(self, bot: Any, service: CaseService) -> None:
        self.bot = bot
        self.service = service

    # ------------------------------------------------------------------ /history

    @app_commands.command(
        name="history",
        description=localized("Show a member's moderation history", "commands.history.description"),
        extras={"permissions": ("moderate_members",)},
    )
    @app_commands.describe(
        member=localized("Whose history to show (defaults to you)", "commands.history.param_member"),
        limit=localized("How many recent cases to show", "commands.history.param_limit"),
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def history(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
        limit: app_commands.Range[int, 1, MAX_HISTORY_LIMIT] | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        target = member or guild.get_member(interaction.user.id)
        if target is None:
            raise UserError("errors.member_not_found")

        t = self.bot.t
        records = await self.service.history(
            guild.id,
            target.id,
            limit=int(limit) if limit is not None else DEFAULT_HISTORY_LIMIT,
        )
        title = t(interaction, "cases.history.title", member=target.display_name)
        if not records:
            await interaction.response.send_message(
                embed=info_embed(title, t(interaction, "cases.history.empty")), ephemeral=True
            )
            return

        embed = info_embed(title, t(interaction, "cases.history.description", count=len(records)))
        for record in records:
            embed.add_field(
                name=f"#{record.case_number} · {t(interaction, f'actions.{record.action}')}",
                value=self._history_lines(interaction, record),
                inline=False,
            )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ /case view

    @case_group.command(
        name="view",
        description=localized("Show one case in detail", "commands.case.view.description"),
        extras={"permissions": ("moderate_members",)},
    )
    @app_commands.describe(number=localized("Case number", "commands.case.param_number"))
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case_view(self, interaction: discord.Interaction, number: int) -> None:
        guild = self._require_guild(interaction)
        record = await self._get_case(guild.id, number)

        t = self.bot.t
        embed = info_embed(
            t(interaction, "cases.view.title", number=record.case_number),
            t(interaction, f"actions.{record.action}"),
        )
        add_fields(
            embed,
            [
                (t(interaction, "cases.view.target"), f"<@{record.target_id}> (`{record.target_id}`)", True),
                (t(interaction, "cases.view.moderator"), f"<@{record.moderator_id}>", True),
                (
                    t(interaction, "cases.view.automated"),
                    t(interaction, "common.yes") if record.automated else t(interaction, "common.no"),
                    True,
                ),
                (
                    t(interaction, "cases.view.duration"),
                    t(interaction, "cases.duration.minutes", minutes=record.duration_seconds // 60)
                    if record.duration_seconds
                    else None,
                    True,
                ),
                (t(interaction, "cases.view.time"), record.created_at, True),
                (
                    t(interaction, "cases.view.status"),
                    t(interaction, "cases.view.status_revoked", time=record.revoked_at or "")
                    if record.is_revoked
                    else t(interaction, "cases.view.status_active"),
                    True,
                ),
                (
                    t(interaction, "cases.view.reason"),
                    record.reason or t(interaction, "common.no_reason"),
                    False,
                ),
                (
                    t(interaction, "cases.view.revoke_reason"),
                    record.revoke_reason if record.is_revoked else None,
                    False,
                ),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ /case revoke

    @case_group.command(
        name="revoke",
        description=localized(
            "Revoke a case and roll back what can be rolled back",
            "commands.case.revoke.description",
        ),
        extras={"permissions": ("moderate_members",)},
    )
    @app_commands.describe(
        number=localized("Case number to revoke", "commands.case.param_number"),
        reason=localized("Why this case is being revoked", "commands.case.revoke.param_reason"),
    )
    @app_commands.checks.has_permissions(moderate_members=True)
    async def case_revoke(
        self,
        interaction: discord.Interaction,
        number: int,
        reason: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        record = await self._get_case(guild.id, number)
        if record.is_revoked:
            raise UserError("cases.already_revoked", number=record.case_number)
        self._check_action_permission(interaction, record)

        t = self.bot.t
        clean_reason = self._clean_reason(reason)
        view = ConfirmView(
            author_id=interaction.user.id,
            confirm_label=t(interaction, "common.confirm"),
            cancel_label=t(interaction, "common.cancel"),
            not_author_message=t(interaction, "common.not_your_button"),
        )
        embed = warn_embed(
            t(interaction, "cases.revoke.confirm_title"),
            t(
                interaction,
                "cases.revoke.confirm_description",
                number=record.case_number,
                action=t(interaction, f"actions.{record.action}"),
                target=f"<@{record.target_id}>",
                reason=clean_reason or t(interaction, "common.no_reason"),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(embed=info_embed(t(interaction, "common.aborted")), view=None)
            return

        note = await self._undo(interaction, guild, record)
        try:
            revoked = await self.service.revoke(
                guild.id,
                record.case_number,
                moderator_id=interaction.user.id,
                reason=clean_reason,
            )
        except CaseAlreadyRevoked:
            # 极端并发：确认期间别人已经撤销了。
            raise UserError("cases.already_revoked", number=record.case_number) from None

        await self._post_case_log(guild, interaction, revoked, note)
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "cases.revoke.done_title"),
                t(
                    interaction,
                    "cases.revoke.done",
                    number=revoked.case_number,
                    action=t(interaction, f"actions.{revoked.action}"),
                    note=note,
                ),
            ),
            view=None,
        )

    # ------------------------------------------------------------------ 内部

    async def _get_case(self, guild_id: int, number: int) -> CaseRecord:
        record = await self.service.get(guild_id, number)
        if record is None:
            raise UserError("cases.not_found", number=number)
        return record

    def _check_action_permission(self, interaction: discord.Interaction, record: CaseRecord) -> None:
        """撤销按动作分别鉴权：能禁言不代表能解封。"""
        flag = required_permission(record.action)
        if flag is None:
            return
        if not getattr(interaction.permissions, flag, False):
            raise UserError(
                "cases.revoke.missing_permission",
                permission=self.bot.t(interaction, f"permissions.{flag}"),
            )

    async def _undo(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        record: CaseRecord,
    ) -> str:
        """在 Discord 侧尽力回滚，返回一句给执行者看的说明。

        回滚失败**不标记撤销**：宁可什么都不改、让执行者重试，也不要留下
        「记录说已撤销、Discord 里其实还封着」的状态。
        """
        t = self.bot.t
        plan = undo_plan(record.action)
        reason = audit_reason(interaction, t(interaction, "cases.revoke.audit_reason", number=record.case_number))

        try:
            if plan == "unban":
                try:
                    await guild.unban(discord.Object(id=record.target_id), reason=reason)
                except discord.NotFound:
                    return t(interaction, "cases.revoke.undo_not_needed")
                return t(interaction, "cases.revoke.undo_unbanned", target=f"`{record.target_id}`")

            if plan == "untimeout":
                member = guild.get_member(record.target_id)
                if member is None:
                    return t(interaction, "cases.revoke.undo_member_gone")
                await member.timeout(None, reason=reason)
                return t(interaction, "cases.revoke.undo_untimeouted", target=member.mention)

            if plan == "clear_warning":
                return t(interaction, "cases.revoke.undo_warning_cleared")
        except discord.HTTPException as exc:
            LOGGER.exception("回滚 case #%s 失败", record.case_number)
            raise UserError("cases.revoke.undo_failed", error=f"{type(exc).__name__}: {exc}") from exc

        return t(interaction, "cases.revoke.undo_not_possible")

    def _history_lines(self, interaction: discord.Interaction, record: CaseRecord) -> str:
        t = self.bot.t
        lines: list[str] = []
        if record.is_revoked:
            lines.append(t(interaction, "cases.history.revoked", time=record.revoked_at or ""))
        lines.append(
            t(
                interaction,
                "cases.history.reason_line",
                reason=record.reason or t(interaction, "common.no_reason"),
            )
        )
        lines.append(t(interaction, "cases.history.moderator_line", moderator=f"<@{record.moderator_id}>"))
        lines.append(t(interaction, "cases.history.time_line", time=record.created_at))
        return "\n".join(lines)

    async def _post_case_log(
        self,
        guild: discord.Guild,
        interaction: discord.Interaction,
        record: CaseRecord,
        note: str,
    ) -> None:
        t = self.bot.t
        embed = info_embed(t(interaction, "cases.log.title"), note)
        add_fields(
            embed,
            [
                (t(interaction, "cases.log.case"), f"#{record.case_number}", True),
                (t(interaction, "cases.log.action"), t(interaction, f"actions.{record.action}"), True),
                (t(interaction, "cases.log.target"), f"<@{record.target_id}>", True),
                (
                    t(interaction, "cases.log.moderator"),
                    f"{interaction.user.mention} (`{interaction.user.id}`)",
                    True,
                ),
                (
                    t(interaction, "cases.log.reason"),
                    record.revoke_reason or t(interaction, "common.no_reason"),
                    False,
                ),
            ],
        )
        embed.set_footer(text=t(interaction, "cases.log.time", time=record.revoked_at or record.created_at))
        await post_to_mod_log(self.bot, guild, embed)

    @staticmethod
    def _clean_reason(reason: str | None) -> str | None:
        if reason is None:
            return None
        cleaned = reason.strip()
        return cleaned[:MAX_REASON_LENGTH] if cleaned else None

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild
