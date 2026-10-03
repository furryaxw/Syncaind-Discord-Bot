"""reaction_roles 的命令层：``/reactionrole`` 一组，外加原始反应事件监听。

事件用 ``on_raw_reaction_*`` 而不是 ``on_reaction_*``：前者不依赖消息是否在缓存里，
重启之后照样能收到——这正是映射必须落库的原因。
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.checks import can_manage_role
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import info_embed, ok_embed

from .service import ReactionRoleService

LOGGER = logging.getLogger("bot.reaction_roles")

MAX_LISTED = 25


@app_commands.guild_only()
class ReactionRolesCog(commands.Cog):
    """表情 ↔ 角色映射。"""

    group = app_commands.Group(
        name="reactionrole",
        description=localized("Let members pick roles by reacting", "commands.reactionrole.description"),
    )

    def __init__(self, bot: Any, service: ReactionRoleService) -> None:
        self.bot = bot
        self.service = service

    # ------------------------------------------------------------------ 配置命令

    @group.command(
        name="add",
        description=localized(
            "Bind an emoji on a message to a role",
            "commands.reactionrole.add.description",
        ),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        message_id=localized("ID of the message to react on", "commands.reactionrole.param_message_id"),
        emoji=localized("The emoji members react with", "commands.reactionrole.param_emoji"),
        role=localized("The role to hand out", "commands.reactionrole.param_role"),
        channel=localized("Where that message is (defaults to here)", "commands.reactionrole.param_channel"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def add(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        role: discord.Role,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        self._require_bot_can_manage_roles(interaction)
        self._check_role(interaction, role)

        parsed_emoji = self._parse_emoji(emoji)
        target_channel = channel or interaction.channel
        message = await self._fetch_message(target_channel, message_id)

        await self.service.add(
            guild.id,
            message_id=message.id,
            channel_id=message.channel.id,
            emoji=parsed_emoji,
            role_id=role.id,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "reaction_roles.add.done_title"),
                self.bot.t(
                    interaction,
                    "reaction_roles.add.done",
                    emoji=parsed_emoji,
                    role=role.mention,
                    url=message.jump_url,
                ),
            ),
            ephemeral=True,
        )

    @group.command(
        name="remove",
        description=localized("Unbind one emoji from a message", "commands.reactionrole.remove.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        message_id=localized("ID of the message", "commands.reactionrole.param_message_id"),
        emoji=localized("The emoji to unbind", "commands.reactionrole.param_emoji"),
        channel=localized("Where that message is (defaults to here)", "commands.reactionrole.param_channel"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def remove(
        self,
        interaction: discord.Interaction,
        message_id: str,
        emoji: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        parsed_emoji = self._parse_emoji(emoji)
        numeric_id = self._parse_message_id(message_id)

        removed = await self.service.remove(guild.id, numeric_id, parsed_emoji)
        t = self.bot.t
        if not removed:
            raise UserError("reaction_roles.remove.not_found", emoji=parsed_emoji, message_id=numeric_id)

        await interaction.response.send_message(
            embed=ok_embed(
                t(interaction, "reaction_roles.remove.done_title"),
                t(
                    interaction,
                    "reaction_roles.remove.done",
                    emoji=parsed_emoji,
                    url=self._jump_url(guild.id, channel, numeric_id),
                ),
            ),
            ephemeral=True,
        )

    @group.command(
        name="list",
        description=localized(
            "List the bindings of a message or of the whole server", "commands.reactionrole.list.description"
        ),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        message_id=localized("Only this message (leave empty for all)", "commands.reactionrole.param_message_id"),
        channel=localized("Where that message is (defaults to here)", "commands.reactionrole.param_channel"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def list_roles(
        self,
        interaction: discord.Interaction,
        message_id: str | None = None,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        if message_id:
            mappings = await self.service.for_message(guild.id, self._parse_message_id(message_id))
        else:
            mappings = await self.service.for_guild(guild.id)

        t = self.bot.t
        title = t(interaction, "reaction_roles.list.title")
        if not mappings:
            await interaction.response.send_message(
                embed=info_embed(title, t(interaction, "reaction_roles.list.empty")), ephemeral=True
            )
            return

        embed = info_embed(title, t(interaction, "reaction_roles.list.description", count=len(mappings)))
        for mapping in mappings[:MAX_LISTED]:
            role = guild.get_role(mapping.role_id)
            lines = [
                t(interaction, "reaction_roles.list.role_line", role=role.mention if role else f"`{mapping.role_id}`"),
                t(interaction, "reaction_roles.list.message_line", url=mapping.jump_url),
            ]
            if role is None:
                lines.append(t(interaction, "reaction_roles.list.stale_role"))
            embed.add_field(name=mapping.emoji, value="\n".join(lines), inline=True)
        if len(mappings) > MAX_LISTED:
            embed.set_footer(text=t(interaction, "reaction_roles.list.truncated", shown=MAX_LISTED))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @group.command(
        name="clear",
        description=localized("Remove every binding of a message", "commands.reactionrole.clear.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        message_id=localized("ID of the message to clean up", "commands.reactionrole.param_message_id"),
        channel=localized("Where that message is (defaults to here)", "commands.reactionrole.param_channel"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def clear(
        self,
        interaction: discord.Interaction,
        message_id: str,
        channel: discord.TextChannel | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        numeric_id = self._parse_message_id(message_id)
        removed = await self.service.clear_message(guild.id, numeric_id)
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "reaction_roles.clear.done_title"),
                self.bot.t(
                    interaction,
                    "reaction_roles.clear.done",
                    count=removed,
                    url=self._jump_url(guild.id, channel, numeric_id),
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 事件

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload: discord.RawReactionActionEvent) -> None:
        await self._apply(payload, adding=True)

    @commands.Cog.listener()
    async def on_raw_reaction_remove(self, payload: discord.RawReactionActionEvent) -> None:
        await self._apply(payload, adding=False)

    async def _apply(self, payload: discord.RawReactionActionEvent, *, adding: bool) -> None:
        if payload.guild_id is None or payload.guild_id != self.bot.settings.guild_id:
            return
        emoji = str(payload.emoji)
        role_id = await self.service.role_id_for(payload.guild_id, payload.message_id, emoji)
        if role_id is None:
            LOGGER.debug("反应没有对应绑定：message=%s emoji=%s", payload.message_id, emoji)
            return

        guild = self.bot.get_guild(payload.guild_id)
        if guild is None:
            LOGGER.warning("收到反应事件但缓存里没有 guild %s", payload.guild_id)
            return
        member = payload.member or guild.get_member(payload.user_id)
        if member is None:
            LOGGER.warning("收到反应事件但拿不到成员 %s（消息 %s）", payload.user_id, payload.message_id)
            return
        if member.bot:
            return

        role = guild.get_role(role_id)
        if role is None:
            # 角色被删了：顺手清掉这条规则，别留着指向空气。
            removed = await self.service.remove_all_for_role(payload.guild_id, role_id)
            LOGGER.warning("角色 %s 已不存在，清理了 %d 条反应角色规则", role_id, removed)
            return

        bot_member = guild.me
        denial = can_manage_role(
            actor=bot_member,
            role=role,
            bot_member=bot_member,
            guild_owner_id=guild.owner_id,
        )
        if denial:
            LOGGER.warning("无法通过反应变更角色 %s：%s", role.name, denial)
            await self._notify(guild, member, "reaction_roles.notify.failed", role=role.name)
            return

        if adding == (role in member.roles):
            # 已经是目标状态：不需要打扰 Discord，也没有变化要告知当事人。
            LOGGER.debug("反应角色无需变更：member=%s role=%s adding=%s", member.id, role.id, adding)
            return

        try:
            if adding:
                await member.add_roles(role, reason=f"reaction role: {emoji}")
            else:
                await member.remove_roles(role, reason=f"reaction role: {emoji}")
        except discord.HTTPException:
            LOGGER.exception("通过反应变更角色失败：member=%s role=%s", member.id, role.id)
            await self._notify(guild, member, "reaction_roles.notify.failed", role=role.name)
            return

        LOGGER.info(
            "反应角色：%s %s %s（消息 %s 表情 %s）",
            member,
            "获得" if adding else "失去",
            role.name,
            payload.message_id,
            emoji,
        )
        await self._notify(
            guild,
            member,
            "reaction_roles.notify.granted" if adding else "reaction_roles.notify.removed",
            role=role.name,
        )

    async def _notify(self, guild: discord.Guild, member: discord.Member, key: str, **kwargs: Any) -> None:
        """给当事人一句反馈——「点了表情」本身没有任何界面回应，不主动说一声就什么都不知道。

        反应事件没有交互对象，所以拿不到对方的客户端语言（``locale`` 只存在于交互上）；
        这里用服务器配置的语言，没配就用 ``DEFAULT_LOCALE``。对方关了私信就静默跳过。
        """
        settings = await self.bot.guild_settings.get(guild.id)
        text = self.bot.i18n.t(settings.locale, key, guild=guild.name, **kwargs)
        try:
            await member.send(text)
        except discord.HTTPException:
            LOGGER.debug("无法私信 %s（可能关闭了私信）", member.id)

    # ------------------------------------------------------------------ 内部

    def _require_bot_can_manage_roles(self, interaction: discord.Interaction) -> None:
        """提前拦一道：配置时机器人没有 Manage Roles，之后每个反应都会失败。"""
        if not interaction.app_permissions.manage_roles:
            raise UserError("reaction_roles.bot_missing_manage_roles")

    def _check_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = self._require_guild(interaction)
        denial = can_manage_role(
            actor=guild.get_member(interaction.user.id),
            role=role,
            bot_member=guild.me,
            guild_owner_id=guild.owner_id,
        )
        if denial:
            raise UserError(denial, role=role.name)

    @staticmethod
    def _parse_emoji(raw: str) -> str:
        """把用户输入规整成与 ``str(payload.emoji)`` 完全一致的字符串。"""
        try:
            parsed = discord.PartialEmoji.from_str(raw.strip())
        except (ValueError, TypeError) as exc:
            raise UserError("reaction_roles.bad_emoji", value=raw) from exc
        if parsed is None or (parsed.id is None and not parsed.name):
            raise UserError("reaction_roles.bad_emoji", value=raw)
        return str(parsed)

    @staticmethod
    def _parse_message_id(raw: str) -> int:
        text = raw.strip()
        if not text.isdigit():
            raise UserError("reaction_roles.bad_message_id", value=raw)
        return int(text)

    async def _fetch_message(self, channel: Any, raw_message_id: str) -> discord.Message:
        message_id = self._parse_message_id(raw_message_id)
        if channel is None or not isinstance(channel, discord.abc.Messageable):
            raise UserError("errors.not_a_text_channel")
        try:
            return await channel.fetch_message(message_id)
        except discord.NotFound as exc:
            raise UserError(
                "reaction_roles.message_not_found", value=message_id, channel=getattr(channel, "mention", "?")
            ) from exc

    @staticmethod
    def _jump_url(guild_id: int, channel: Any, message_id: int) -> str:
        channel_id = getattr(channel, "id", 0)
        return f"https://discord.com/channels/{guild_id}/{channel_id}/{message_id}"

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild
