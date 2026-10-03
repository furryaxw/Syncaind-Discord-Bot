"""tools 的命令层：/serverinfo /userinfo /permissions。"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.checks import can_act_on, can_manage_role
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import add_fields, info_embed

LOGGER = logging.getLogger("bot.tools")

# 权限诊断里逐项检查的能力：(权限位属性名, i18n 键后缀)
MEMBER_PERMISSIONS: tuple[str, ...] = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_messages",
    "manage_nicknames",
    "manage_webhooks",
    "mention_everyone",
    "view_audit_log",
)

BOT_PERMISSIONS: tuple[str, ...] = (
    "view_channel",
    "send_messages",
    "embed_links",
    "manage_messages",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "moderate_members",
)


@app_commands.guild_only()
class ToolsCog(commands.Cog):
    """只读的信息与诊断命令。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="serverinfo",
        description=localized("Show this server's basic information", "commands.serverinfo.description"),
    )
    async def serverinfo(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        t = self.bot.t
        humans = sum(1 for member in guild.members if not member.bot)
        text_channels = sum(1 for channel in guild.channels if isinstance(channel, discord.TextChannel))
        voice_channels = sum(1 for channel in guild.channels if isinstance(channel, discord.VoiceChannel))

        embed = info_embed(guild.name, guild.description)
        add_fields(
            embed,
            [
                (t(interaction, "tools.serverinfo.id"), f"`{guild.id}`", True),
                (
                    t(interaction, "tools.serverinfo.owner"),
                    f"<@{guild.owner_id}>" if guild.owner_id else None,
                    True,
                ),
                (
                    t(interaction, "tools.serverinfo.created"),
                    discord.utils.format_dt(guild.created_at, "F"),
                    True,
                ),
                (
                    t(interaction, "tools.serverinfo.members"),
                    t(
                        interaction,
                        "tools.serverinfo.members_value",
                        total=guild.member_count or len(guild.members),
                        humans=humans,
                    ),
                    True,
                ),
                (
                    t(interaction, "tools.serverinfo.channels"),
                    t(
                        interaction,
                        "tools.serverinfo.channels_value",
                        text=text_channels,
                        voice=voice_channels,
                    ),
                    True,
                ),
                (t(interaction, "tools.serverinfo.roles"), str(len(guild.roles)), True),
                (
                    t(interaction, "tools.serverinfo.boosts"),
                    t(
                        interaction,
                        "tools.serverinfo.boosts_value",
                        tier=guild.premium_tier,
                        count=guild.premium_subscription_count,
                    ),
                    True,
                ),
                (
                    t(interaction, "tools.serverinfo.verification"),
                    str(guild.verification_level).replace("_", " "),
                    True,
                ),
            ],
        )
        if guild.icon is not None:
            embed.set_thumbnail(url=guild.icon.url)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="userinfo",
        description=localized("Show information about a member", "commands.userinfo.description"),
    )
    @app_commands.describe(member=localized("Whose info to show (defaults to you)", "commands.userinfo.param_member"))
    async def userinfo(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        target = member or guild.get_member(interaction.user.id)
        if target is None:
            raise UserError("errors.member_not_found")

        t = self.bot.t
        embed = info_embed(target.display_name, target.mention)
        embed.set_thumbnail(url=target.display_avatar.url)

        role_mentions = [role.mention for role in reversed(target.roles) if not role.is_default()]
        roles_text = " ".join(role_mentions) if role_mentions else t(interaction, "tools.userinfo.no_roles")
        if len(roles_text) > 1000:
            roles_text = roles_text[:997] + "…"

        add_fields(
            embed,
            [
                (t(interaction, "tools.userinfo.id"), f"`{target.id}`", True),
                (t(interaction, "tools.userinfo.nickname"), target.nick, True),
                (
                    t(interaction, "tools.userinfo.bot"),
                    t(interaction, "common.yes") if target.bot else t(interaction, "common.no"),
                    True,
                ),
                (
                    t(interaction, "tools.userinfo.created"),
                    discord.utils.format_dt(target.created_at, "F"),
                    True,
                ),
                (
                    t(interaction, "tools.userinfo.joined"),
                    discord.utils.format_dt(target.joined_at, "F") if target.joined_at else None,
                    True,
                ),
                (
                    t(interaction, "tools.userinfo.timed_out"),
                    discord.utils.format_dt(target.timed_out_until, "F") if target.timed_out_until else None,
                    True,
                ),
                (t(interaction, "tools.userinfo.top_role"), target.top_role.mention, True),
                (t(interaction, "tools.userinfo.roles"), roles_text, False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @app_commands.command(
        name="permissions",
        description=localized(
            "Diagnose why something is or is not allowed",
            "commands.permissions.description",
        ),
    )
    @app_commands.describe(
        member=localized(
            "Whose permissions to inspect (defaults to you)",
            "commands.permissions.param_member",
        )
    )
    async def permissions(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        target = member or guild.get_member(interaction.user.id)
        if target is None:
            raise UserError("errors.member_not_found")

        t = self.bot.t
        embed = info_embed(
            t(interaction, "tools.permissions.title", member=target.display_name),
            t(interaction, "tools.permissions.description"),
        )

        granted, missing = _split_permissions(target.guild_permissions, MEMBER_PERMISSIONS, t, interaction)
        embed.add_field(
            name=t(interaction, "tools.permissions.granted"),
            value=granted or t(interaction, "tools.permissions.none"),
            inline=False,
        )
        embed.add_field(
            name=t(interaction, "tools.permissions.missing"),
            value=missing or t(interaction, "tools.permissions.none"),
            inline=False,
        )

        channel = interaction.channel
        if isinstance(channel, discord.abc.GuildChannel):
            bot_permissions = channel.permissions_for(guild.me)
            bot_granted, bot_missing = _split_permissions(bot_permissions, BOT_PERMISSIONS, t, interaction)
            embed.add_field(
                name=t(interaction, "tools.permissions.bot_in_channel"),
                value=bot_missing or t(interaction, "tools.permissions.all_present"),
                inline=False,
            )
            LOGGER.debug("已授予的机器人权限：%s", bot_granted)

        denial = can_act_on(
            actor=guild.get_member(interaction.user.id),
            target=target,
            bot_member=guild.me,
            guild_owner_id=guild.owner_id,
        )
        verdict = (
            t(interaction, "tools.permissions.punish_ok")
            if denial is None
            else t(interaction, denial, target=target.display_name)
        )
        embed.add_field(
            name=t(interaction, "tools.permissions.punish_verdict"),
            value=verdict,
            inline=False,
        )

        # 「没有可处罚对象」是最常见的困惑来源（自己不能罚自己、owner 不能罚），
        # 所以把它单独说清楚，而不是让用户对着一句「不能对自己执行」发呆。
        candidates = _punishable_members(guild, actor_id=interaction.user.id)
        embed.add_field(
            name=t(interaction, "tools.permissions.targets"),
            value=(
                t(interaction, "tools.permissions.targets_value", count=len(candidates))
                if candidates
                else t(interaction, "tools.permissions.no_target")
            ),
            inline=False,
        )

        # ``is_default()`` 是 Role 的方法，不是 Member 的——这里要问的是「最高角色是不是 @everyone」。
        top_role = target.top_role
        if not top_role.is_default():
            role_denial = can_manage_role(
                actor=guild.get_member(interaction.user.id),
                role=top_role,
                bot_member=guild.me,
                guild_owner_id=guild.owner_id,
            )
            embed.add_field(
                name=t(interaction, "tools.permissions.role_verdict"),
                value=(
                    t(interaction, "tools.permissions.role_ok", role=top_role.name)
                    if role_denial is None
                    else t(interaction, role_denial, role=top_role.name)
                ),
                inline=False,
            )

        await interaction.response.send_message(embed=embed, ephemeral=True)

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild


def _punishable_members(guild: discord.Guild, *, actor_id: int) -> list[discord.Member]:
    """当前真正可以被处罚的成员。

    排除发起者自己、机器人自己与服务器拥有者——这三种身份都会被守卫挡住，
    所以要测处罚命令的人必须知道「不是我的权限不够，而是没有目标」。
    """
    excluded = {actor_id, guild.owner_id}
    if guild.me is not None:
        excluded.add(guild.me.id)
    return [member for member in guild.members if member.id not in excluded]


def _split_permissions(
    permissions: discord.Permissions,
    names: tuple[str, ...],
    t: Any,
    interaction: discord.Interaction,
) -> tuple[str, str]:
    """把权限位切成「已拥有」和「未拥有」两段可读文本。"""
    granted: list[str] = []
    missing: list[str] = []
    for name in names:
        label = t(interaction, f"permissions.{name}")
        if getattr(permissions, name, False):
            granted.append(f"✅ {label}")
        else:
            missing.append(f"❌ {label}")
    return "\n".join(granted), "\n".join(missing)
