"""access_roles：把 SMAS 的**权限节点**同步成 Discord 角色。

三条定下来的原则（用户选的）：

1. 粒度是**具体节点 → 角色**（不是按 Team、也不是按模板）；
2. **SMAS 是唯一权威** —— 只读它、只往 Discord 写，绝不反过来改服务器上的授权；
3. 角色由服务器预先建好，机器人**只做绑定**（`/access bind`）。

由此得出本模块最重要的一条安全边界：**只动绑定表里出现过的角色**。
某个人身上别的角色（哪怕是手动发的）永远不会被这次同步碰掉 —— 这是「撤角色」这种
破坏性动作唯一站得住的理由。

第二条安全边界：**读失败绝不等于「他没有权限」**。某个成员的有效节点读不到（网络、
权限、服务端拒绝）时，跳过这个人并如实报告，而不是按空集合去撤他的角色 ——
否则一次网络抖动就会把所有人的角色撤光。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.checks import can_manage_role
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import info_embed, ok_embed
from bot.integrations.github import GitHubAccountStore
from bot.integrations.smas import (
    AccessSource,
    AccessSourceError,
    NodePatternError,
    NodeRoleBindingStore,
    UnconfiguredAccessSource,
    desired_role_ids,
)

LOGGER = logging.getLogger("bot.access_roles")
SYNC_REASON = "SMAS 权限同步"


@dataclass
class SyncReport:
    """一次同步的结果。``failed`` 是**读不到权限**的人数（跳过，不是「没有权限」）。"""

    checked: int = 0
    added: int = 0
    removed: int = 0
    skipped: int = 0
    failed: int = 0
    details: list[str] = field(default_factory=list)

    @property
    def changed(self) -> int:
        return self.added + self.removed


@app_commands.guild_only()
class AccessRolesCog(commands.Cog):
    """权限节点 → Discord 角色。"""

    access_group = app_commands.Group(
        name="access",
        description=localized("Sync SMAS permission nodes into Discord roles", "commands.access.description"),
    )

    def __init__(
        self,
        bot: Any,
        store: NodeRoleBindingStore,
        accounts: GitHubAccountStore,
        source: AccessSource | None = None,
    ) -> None:
        self.bot = bot
        self.store = store
        self.accounts = accounts
        self._source = source or UnconfiguredAccessSource()

    # ------------------------------------------------------------------ /access bind

    @access_group.command(
        name="bind",
        description=localized("Bind a permission node to a role", "commands.access.bind.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        node=localized("Node or pattern, e.g. team.acme.packages.read or team.acme.*", "commands.access.param_node"),
        role=localized("Role to grant while the node is held", "commands.access.param_role"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def access_bind(self, interaction: discord.Interaction, node: str, role: discord.Role) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        self._check_hierarchy(interaction, guild, role)

        try:
            added = await self.store.bind(guild.id, pattern=node, role_id=role.id, created_by=interaction.user.id)
        except NodePatternError as exc:
            raise UserError("access.bad_node", value=node, reason=str(exc)) from exc

        t = self.bot.t
        await interaction.response.send_message(
            embed=ok_embed(
                t(interaction, "access.bind.added_title" if added else "access.bind.exists_title"),
                t(
                    interaction,
                    "access.bind.added" if added else "access.bind.exists",
                    node=node.strip().lower(),
                    role=role.mention,
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /access unbind

    @access_group.command(
        name="unbind",
        description=localized("Remove a node binding", "commands.access.unbind.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(
        node=localized("Node or pattern", "commands.access.param_node"),
        role=localized("Only this role; omit to remove every role for the node", "commands.access.param_role"),
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def access_unbind(
        self, interaction: discord.Interaction, node: str, role: discord.Role | None = None
    ) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        try:
            removed = await self.store.unbind(guild.id, pattern=node, role_id=role.id if role else None)
        except NodePatternError as exc:
            raise UserError("access.bad_node", value=node, reason=str(exc)) from exc
        if not removed:
            raise UserError("access.unbind.none", node=node.strip().lower())

        t = self.bot.t
        await interaction.response.send_message(
            embed=ok_embed(
                t(interaction, "access.unbind.title"),
                t(interaction, "access.unbind.done", node=node.strip().lower(), count=removed),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /access list

    @access_group.command(
        name="list",
        description=localized("Show node → role bindings", "commands.access.list.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.checks.has_permissions(manage_roles=True)
    async def access_list(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        bindings = await self.store.all(guild.id)
        t = self.bot.t
        title = t(interaction, "access.list.title")
        if not bindings:
            await interaction.response.send_message(
                embed=info_embed(title, t(interaction, "access.list.empty")), ephemeral=True
            )
            return

        lines = [
            t(
                interaction,
                "access.list.entry",
                node=binding.node_pattern,
                role=f"<@&{binding.role_id}>",
            )
            for binding in bindings
        ]
        note = t(
            interaction,
            "access.list.configured" if self.configured else "smas.not_configured",
        )
        await interaction.response.send_message(
            embed=info_embed(title, "\n".join(lines) + "\n\n" + note), ephemeral=True
        )

    # ------------------------------------------------------------------ /access sync

    @access_group.command(
        name="sync",
        description=localized("Reconcile roles with SMAS now", "commands.access.sync.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(dry_run=localized("Only report what would change", "commands.access.param_dry_run"))
    @app_commands.checks.has_permissions(manage_roles=True)
    async def access_sync(self, interaction: discord.Interaction, dry_run: bool = False) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        if not self.configured:
            raise UserError("smas.not_configured")

        await interaction.response.defer(ephemeral=True)
        report = await self.sync_guild(guild, dry_run=dry_run)
        t = self.bot.t
        await interaction.edit_original_response(
            embed=info_embed(
                t(interaction, "access.sync.dry_title" if dry_run else "access.sync.title"),
                t(
                    interaction,
                    "access.sync.report",
                    checked=report.checked,
                    added=report.added,
                    removed=report.removed,
                    skipped=report.skipped,
                    failed=report.failed,
                )
                + ("\n\n" + t(interaction, "access.sync.dry_note") if dry_run else ""),
            )
        )

    # ------------------------------------------------------------------ /access status

    @access_group.command(
        name="status",
        description=localized("Why does this member have those roles", "commands.access.status.description"),
        extras={"permissions": ("manage_roles",)},
    )
    @app_commands.describe(member=localized("Member to inspect", "commands.access.param_member"))
    @app_commands.checks.has_permissions(manage_roles=True)
    async def access_status(self, interaction: discord.Interaction, member: discord.Member) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        if not self.configured:
            raise UserError("smas.not_configured")

        account = await self.accounts.get(guild.id, member.id)
        if account is None:
            raise UserError("access.status.not_linked", member=member.display_name)

        await interaction.response.defer(ephemeral=True)
        t = self.bot.t
        try:
            nodes = await self._source.effective_nodes(str(account.github_user_id))
        except AccessSourceError as exc:
            raise UserError("access.status.read_failed", member=member.display_name, error=str(exc)) from exc

        bindings = await self.store.pairs(guild.id)
        matched = desired_role_ids(nodes.nodes, bindings)
        title = t(interaction, "access.status.title", member=member.display_name)
        body = "\n".join(
            [
                t(interaction, "access.status.account", login=account.github_login),
                t(interaction, "access.status.nodes", count=len(nodes.nodes)),
                t(
                    interaction,
                    "access.status.roles",
                    roles="、".join(f"<@&{role_id}>" for role_id in sorted(matched))
                    or t(interaction, "access.status.none"),
                ),
                "",
                t(
                    interaction,
                    "access.status.node_list",
                    nodes="、".join(f"`{node}`" for node in sorted(nodes.nodes)) or "—",
                ),
            ]
        )
        await interaction.edit_original_response(embed=info_embed(title, body))

    # ------------------------------------------------------------------ 同步引擎

    async def sync_guild(self, guild: discord.Guild, *, dry_run: bool = False) -> SyncReport:
        """把每个人的角色对齐到他**现在**的 SMAS 节点。

        只动绑定表里出现过的角色；读不到节点的人跳过（绝不当作「没权限」）。
        """
        report = SyncReport()
        bindings = await self.store.pairs(guild.id)
        bound_roles = await self.store.bound_role_ids(guild.id)
        accounts = await self.accounts.all(guild.id)
        mode = "dry-run" if dry_run else "apply"
        LOGGER.info(
            "开始同步：guild=%s 已绑定成员=%d 绑定规则=%d mode=%s", guild.id, len(accounts), len(bindings), mode
        )

        for account in accounts:
            member = guild.get_member(account.discord_user_id)
            if member is None:
                report.skipped += 1  # 人不在服务器里了
                continue

            try:
                nodes = await self._source.effective_nodes(str(account.github_user_id))
            except AccessSourceError as exc:
                # ★ 读失败 ≠ 没有权限：跳过，不撤角色。
                LOGGER.warning("读不到 %s 的权限，跳过：%s", account.github_login, exc)
                report.failed += 1
                continue

            report.checked += 1
            desired = desired_role_ids(nodes.nodes, bindings)
            current = {role.id for role in member.roles if role.id in bound_roles}

            for role_id in sorted(desired - current):
                if await self._apply(guild, member, role_id, add=True, dry_run=dry_run, report=report):
                    report.added += 1
            for role_id in sorted(current - desired):
                if await self._apply(guild, member, role_id, add=False, dry_run=dry_run, report=report):
                    report.removed += 1
        return report

    async def _apply(
        self,
        guild: discord.Guild,
        member: discord.Member,
        role_id: int,
        *,
        add: bool,
        dry_run: bool,
        report: SyncReport,
    ) -> bool:
        """加/撤一个角色。层级不够就跳过并如实计数（不报错中断）。"""
        role = guild.get_role(role_id)
        if role is None:
            LOGGER.warning("绑定里的角色 %s 已不存在，跳过", role_id)
            report.skipped += 1
            return False
        denial = can_manage_role(actor=guild.me, role=role, bot_member=guild.me, guild_owner_id=guild.owner_id)
        if denial:
            LOGGER.warning("不能操作角色 %s：%s", role.name, denial)
            report.skipped += 1
            return False

        report.details.append(f"{'add' if add else 'remove'} {member.id} {role.id}")
        if dry_run:
            return True
        try:
            if add:
                await member.add_roles(role, reason=SYNC_REASON)
            else:
                await member.remove_roles(role, reason=SYNC_REASON)
        except discord.HTTPException:
            LOGGER.exception("同步角色失败：member=%s role=%s add=%s", member.id, role.id, add)
            report.skipped += 1
            return False
        return True

    # ------------------------------------------------------------------ 内部

    @property
    def configured(self) -> bool:
        return not isinstance(self._source, UnconfiguredAccessSource)

    async def _require_linked(self, interaction: discord.Interaction) -> None:
        guild = self._require_guild(interaction)
        if await self.accounts.get(guild.id, interaction.user.id) is None:
            raise UserError("github.link_required")

    def _check_hierarchy(self, interaction: discord.Interaction, guild: discord.Guild, role: discord.Role) -> None:
        denial = can_manage_role(
            actor=guild.get_member(interaction.user.id),
            role=role,
            bot_member=guild.me,
            guild_owner_id=guild.owner_id,
        )
        if denial:
            raise UserError(denial, role=role.name)

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild
