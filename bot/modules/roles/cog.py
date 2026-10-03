"""roles：角色管理（建、删、改名、改色、权限位、批量授予与移除）。

参数一律用 discord.py 的转换器（``discord.Role`` / ``discord.Member``）解析，
批量成员则用一串 id/提及——Discord 的斜杠命令没有可变参数，这是唯一可行且可校验的做法。
"""

from __future__ import annotations

import logging
import re
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.audit import audit_reason
from bot.core.checks import can_manage_role
from bot.core.errors import UserError
from bot.core.permission_flags import PermissionParseError, apply_overwrite, parse_flags
from bot.core.translator import localized
from bot.core.ui import ConfirmView, add_fields, ask_confirmation, info_embed, ok_embed, warn_embed

LOGGER = logging.getLogger("bot.roles")

# 成员列表的分隔符：逗号、空白、中英文标点都收。
_MEMBER_SPLIT = re.compile(r"[,\s，、]+")

# /role info 里展示的「关键权限」——不全列（60 多项列不下），只列管理员真正关心的。
KEY_PERMISSIONS = (
    "administrator",
    "manage_guild",
    "manage_roles",
    "manage_channels",
    "kick_members",
    "ban_members",
    "moderate_members",
    "manage_messages",
    "mention_everyone",
)

MAX_REASON_LENGTH = 480


class MemberParseError(ValueError):
    """用户输入的成员列表里有不是 ID/提及的片段。"""

    def __init__(self, value: str) -> None:
        super().__init__(f"无法解析为成员：{value}")
        self.value = value


def parse_colour(text: str) -> discord.Colour:
    """把 ``#ff0000`` / ``ff0000`` / ``0xff0000`` / ``f00`` 解析成颜色。"""
    cleaned = text.strip()
    if cleaned.startswith("#"):
        cleaned = cleaned[1:]
    elif cleaned.lower().startswith("0x"):
        cleaned = cleaned[2:]
    if len(cleaned) == 3 and all(char in "0123456789abcdefABCDEF" for char in cleaned):
        cleaned = "".join(char * 2 for char in cleaned)
    if len(cleaned) != 6 or not all(char in "0123456789abcdefABCDEF" for char in cleaned):
        raise ValueError(f"不是合法的十六进制颜色：{text}")
    return discord.Colour(int(cleaned, 16))


def parse_member_ids(text: str) -> tuple[int, ...]:
    """把 ``"<@1> 2,3"`` 解析成 ``(1, 2, 3)``（去重保序）。非数字片段抛错。"""
    if not text or not text.strip():
        return ()
    ids: list[int] = []
    for part in _MEMBER_SPLIT.split(text.strip()):
        cleaned = part.strip("<@!>&")
        if not cleaned.isdigit():
            raise MemberParseError(part)
        ids.append(int(cleaned))
    seen: dict[int, None] = {}
    for value in ids:
        seen.setdefault(value, None)
    return tuple(seen)


@app_commands.guild_only()
class RolesCog(commands.Cog):
    """角色管理。"""

    role_group = app_commands.Group(
        name="role",
        description=localized("Create and manage roles", "commands.role.description"),
    )

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    # ------------------------------------------------------------------ 建 / 删 / 改名 / 改色

    @role_group.command(
        name="create",
        description=localized("Create a role", "commands.role.create.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        name=localized("Role name", "commands.role.create.param_name"),
        color=localized("Hex colour, e.g. ff8800", "commands.role.create.param_color"),
        hoist=localized("Show members with this role separately", "commands.role.create.param_hoist"),
        mentionable=localized("Allow @mentioning this role", "commands.role.create.param_mentionable"),
        allow=localized("Permissions to grant, separated by commas", "commands.role.create.param_allow"),
        deny=localized("Permissions to keep off, separated by commas", "commands.role.create.param_deny"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def create(
        self,
        interaction: discord.Interaction,
        name: str,
        color: str | None = None,
        hoist: bool = False,
        mentionable: bool = False,
        allow: str | None = None,
        deny: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        allow_flags, deny_flags = self._parse_permissions(interaction, allow, deny)
        try:
            colour = parse_colour(color) if color else discord.Colour.default()
        except ValueError as exc:
            raise UserError("roles.bad_color", value=str(exc)) from exc

        role = await guild.create_role(
            name=name,
            colour=colour,
            hoist=hoist,
            mentionable=mentionable,
            permissions=apply_overwrite(discord.Permissions.none(), allow=allow_flags, deny=deny_flags),
            reason=audit_reason(interaction),
        )
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "roles.create.done_title"),
                self.bot.t(interaction, "roles.create.done", role=role.mention),
            ),
            ephemeral=True,
        )

    @role_group.command(
        name="delete",
        description=localized("Delete a role", "commands.role.delete.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        role=localized("The role to delete", "commands.role.delete.param_role"),
        reason=localized("Why it is being deleted", "commands.role.delete.param_reason"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def delete(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        reason: str | None = None,
    ) -> None:
        self._check_role(interaction, role)
        t = self.bot.t
        clean_reason = self._clean_reason(reason)

        view = self._confirm_view(interaction)
        embed = warn_embed(
            t(interaction, "roles.confirm.title"),
            t(
                interaction,
                "roles.confirm.delete",
                role=role.name,
                count=len(role.members),
                reason=clean_reason or t(interaction, "common.no_reason"),
            ),
        )
        if not await ask_confirmation(interaction, embed=embed, view=view):
            await interaction.edit_original_response(embed=info_embed(t(interaction, "common.aborted")), view=None)
            return

        await role.delete(reason=audit_reason(interaction, clean_reason))
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "roles.delete.done_title"),
                t(interaction, "roles.delete.done", role=role.name),
            ),
            view=None,
        )

    @role_group.command(
        name="rename",
        description=localized("Rename a role", "commands.role.rename.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        role=localized("The role to rename", "commands.role.rename.param_role"),
        name=localized("The new name", "commands.role.rename.param_name"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def rename(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        name: str,
    ) -> None:
        self._check_role(interaction, role)
        await role.edit(name=name, reason=audit_reason(interaction))
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "roles.rename.done_title"),
                self.bot.t(interaction, "roles.rename.done", role=role.mention, name=name),
            ),
            ephemeral=True,
        )

    @role_group.command(
        name="color",
        description=localized("Change a role's colour", "commands.role.color.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        role=localized("The role to recolour", "commands.role.color.param_role"),
        value=localized("Hex colour, e.g. ff8800", "commands.role.color.param_value"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def color(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        value: str,
    ) -> None:
        self._check_role(interaction, role)
        try:
            colour = parse_colour(value)
        except ValueError as exc:
            raise UserError("roles.bad_color", value=str(exc)) from exc
        await role.edit(colour=colour, reason=audit_reason(interaction))
        await interaction.response.send_message(
            embed=ok_embed(
                self.bot.t(interaction, "roles.color.done_title"),
                self.bot.t(interaction, "roles.color.done", role=role.mention, color=f"#{colour.value:06X}"),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 权限位

    @role_group.command(
        name="permissions",
        description=localized(
            "Grant or deny specific permissions on a role",
            "commands.role.permissions.description",
        ),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        role=localized("The role to change", "commands.role.permissions.param_role"),
        allow=localized("Permissions to grant, separated by commas", "commands.role.permissions.param_allow"),
        deny=localized("Permissions to remove, separated by commas", "commands.role.permissions.param_deny"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def permissions(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        allow: str | None = None,
        deny: str | None = None,
    ) -> None:
        self._check_role(interaction, role)
        allow_flags, deny_flags = self._parse_permissions(interaction, allow, deny)
        if not allow_flags and not deny_flags:
            raise UserError("errors.no_permissions_given")

        # 只改点名的位：没提到的保持原样，避免一次调用把角色其它权限悄悄清掉。
        updated = apply_overwrite(role.permissions, allow=allow_flags, deny=deny_flags)
        await role.edit(permissions=updated, reason=audit_reason(interaction))

        t = self.bot.t
        await interaction.response.send_message(
            embed=ok_embed(
                t(interaction, "roles.permissions.done_title"),
                t(
                    interaction,
                    "roles.permissions.done",
                    role=role.mention,
                    allow=self._flags_text(interaction, allow_flags),
                    deny=self._flags_text(interaction, deny_flags),
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 批量授予 / 移除

    @role_group.command(
        name="grant",
        description=localized(
            "Give a role to several members at once",
            "commands.role.grant.description",
        ),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        role=localized("The role to give", "commands.role.grant.param_role"),
        members=localized(
            "Members as mentions or IDs, separated by spaces or commas",
            "commands.role.grant.param_members",
        ),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def grant(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        members: str,
    ) -> None:
        await self._apply_role(interaction, role, members, adding=True)

    @role_group.command(
        name="revoke",
        description=localized(
            "Take a role away from several members at once",
            "commands.role.revoke.description",
        ),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        role=localized("The role to take away", "commands.role.revoke.param_role"),
        members=localized(
            "Members as mentions or IDs, separated by spaces or commas",
            "commands.role.revoke.param_members",
        ),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def revoke(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        members: str,
    ) -> None:
        await self._apply_role(interaction, role, members, adding=False)

    # ------------------------------------------------------------------ 信息

    @role_group.command(
        name="info",
        description=localized("Show a role's settings", "commands.role.info.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(role=localized("The role to inspect", "commands.role.info.param_role"))
    @app_commands.checks.has_permissions(manage_roles=True)
    async def info(self, interaction: discord.Interaction, role: discord.Role) -> None:
        t = self.bot.t
        embed = info_embed(
            role.name,
            t(
                interaction,
                "roles.info.managed" if role.managed else "roles.info.description",
                role=role.mention,
            ),
        )
        granted = [
            t(interaction, f"permissions.{flag}") for flag in KEY_PERMISSIONS if getattr(role.permissions, flag, False)
        ]
        add_fields(
            embed,
            [
                (t(interaction, "roles.info.id"), f"`{role.id}`", True),
                (t(interaction, "roles.info.color"), f"#{role.colour.value:06X}", True),
                (t(interaction, "roles.info.position"), str(role.position), True),
                (t(interaction, "roles.info.members"), str(len(role.members)), True),
                (
                    t(interaction, "roles.info.hoist"),
                    t(interaction, "common.yes") if role.hoist else t(interaction, "common.no"),
                    True,
                ),
                (
                    t(interaction, "roles.info.mentionable"),
                    t(interaction, "common.yes") if role.mentionable else t(interaction, "common.no"),
                    True,
                ),
                (
                    t(interaction, "roles.info.permissions"),
                    "\n".join(granted) if granted else t(interaction, "common.none"),
                    False,
                ),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    # ------------------------------------------------------------------ 内部

    async def _apply_role(
        self,
        interaction: discord.Interaction,
        role: discord.Role,
        members_text: str,
        *,
        adding: bool,
    ) -> None:
        guild = self._require_guild(interaction)
        self._check_role(interaction, role)
        resolved, missing = await self._resolve_members(guild, members_text)
        if not resolved and not missing:
            raise UserError("roles.no_members")

        reason = audit_reason(interaction)
        applied: list[str] = []
        unchanged: list[str] = []
        failed: list[str] = []
        for member in resolved:
            already = role in member.roles
            if already == adding:
                unchanged.append(member.display_name)
                continue
            try:
                if adding:
                    await member.add_roles(role, reason=reason)
                else:
                    await member.remove_roles(role, reason=reason)
                applied.append(member.display_name)
            except discord.HTTPException:
                LOGGER.exception("对成员 %s 变更角色 %s 失败", member.id, role.id)
                failed.append(member.display_name)

        t = self.bot.t
        embed = ok_embed(
            t(interaction, "roles.grant.done_title" if adding else "roles.revoke.done_title"),
            t(
                interaction,
                "roles.apply.done",
                role=role.mention,
                applied=len(applied),
                unchanged=len(unchanged),
            ),
        )
        embed = add_fields(
            embed,
            [
                (
                    t(interaction, "roles.apply.applied_names"),
                    ", ".join(applied) or None,
                    False,
                ),
                (
                    t(interaction, "roles.apply.unchanged_names"),
                    ", ".join(unchanged) or None,
                    False,
                ),
                (
                    t(interaction, "roles.apply.missing_names"),
                    ", ".join(f"`{value}`" for value in missing) or None,
                    False,
                ),
                (t(interaction, "roles.apply.failed_names"), ", ".join(failed) or None, False),
            ],
        )
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _resolve_members(self, guild: discord.Guild, members_text: str) -> tuple[list[discord.Member], list[int]]:
        try:
            member_ids = parse_member_ids(members_text)
        except MemberParseError as exc:
            raise UserError("roles.bad_member", value=exc.value) from exc

        found: list[discord.Member] = []
        missing: list[int] = []
        for member_id in member_ids:
            member = guild.get_member(member_id)
            if member is None:
                try:
                    member = await guild.fetch_member(member_id)
                except discord.HTTPException:
                    member = None
            if member is None:
                missing.append(member_id)
            else:
                found.append(member)
        return found, missing

    def _check_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        """角色可改性：不由集成管理、不是 @everyone、且低于机器人与执行者的最高角色。"""
        guild = self._require_guild(interaction)
        denial = can_manage_role(
            actor=guild.get_member(interaction.user.id),
            role=role,
            bot_member=guild.me,
            guild_owner_id=guild.owner_id,
        )
        if denial:
            raise UserError(denial, role=role.name)

    def _parse_permissions(
        self, interaction: discord.Interaction, allow: str | None, deny: str | None
    ) -> tuple[tuple[str, ...], tuple[str, ...]]:
        try:
            return parse_flags(allow), parse_flags(deny)
        except PermissionParseError as exc:
            raise UserError(
                "errors.unknown_permissions",
                permissions=", ".join(f"`{name}`" for name in exc.unknown),
                valid=self.bot.t(interaction, "errors.valid_permissions_hint"),
            ) from exc

    def _flags_text(self, interaction: discord.Interaction, flags: tuple[str, ...]) -> str:
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
