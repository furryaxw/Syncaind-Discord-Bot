"""框架级 /help：列出所有命令，并按调用者的实际权限标出可用性。

这里刻意不复刻一份命令清单，而是**直接读命令树**，并且用
``get_translated_payload`` 取「真正会发给 Discord 的那份 payload」来拿文案——
这样 /help 显示的就是用户在 Discord 里实际看到的名字与描述，
不会出现「帮助里写的和命令实际长的」不一致。
"""

from __future__ import annotations

import inspect
import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .errors import UserError
from .translator import localized
from .ui import info_embed

LOGGER = logging.getLogger("bot.framework")

FIELD_VALUE_LIMIT = 1000
MAX_CHOICES = 25


def listable_commands(tree: app_commands.CommandTree) -> list[tuple[str, app_commands.Command]]:
    """概览用：分组不单列，而是把它的子命令摊开。

    返回 ``[(斜杠调用名, 命令对象)]``，名字是形如 ``module list`` 的完整路径。
    """
    listed: list[tuple[str, app_commands.Command]] = []
    for command in tree.get_commands():
        if isinstance(command, app_commands.Group):
            listed.extend((f"{command.name} {child.name}", child) for child in command.commands)
        else:
            listed.append((command.name, command))
    return listed


def findable_commands(tree: app_commands.CommandTree) -> list[tuple[str, app_commands.Command]]:
    """查找用：分组自己也要能被 ``/help module`` 解释，所以列进来。"""
    listed: list[tuple[str, app_commands.Command]] = []
    for command in tree.get_commands():
        listed.append((command.name, command))
        if isinstance(command, app_commands.Group):
            listed.extend((f"{command.name} {child.name}", child) for child in command.commands)
    return listed


async def command_payload(bot: Any, command: app_commands.Command) -> dict[str, Any]:
    """取该命令会发给 Discord 的 payload（含各语言的本地化结果）。

    ``tree.translator`` 没设置时退回未翻译的 ``to_dict``——测试里会走到这条路径。
    """
    translator = getattr(bot.tree, "translator", None)
    if translator is None:
        return command.to_dict(bot.tree)
    return await command.get_translated_payload(bot.tree, translator)


def localized_text(payload: dict[str, Any], field: str, locale: str) -> str:
    """从 payload 里取某个字段在当前语言下的文本，取不到就用英文原文。"""
    localizations = payload.get(f"{field}_localizations") or {}
    return localizations.get(locale) or payload.get(field) or ""


async def command_is_usable(interaction: discord.Interaction, command: app_commands.Command) -> bool:
    """判断调用者当前能不能用这条命令。

    * 分组命令本身没有检查，按「有任何子命令可用」算；
    * 单个检查失败一律当作不可用——帮助列表不该因为某个检查抛异常就整页打不开。
    """
    if isinstance(command, app_commands.Group):
        return any([await command_is_usable(interaction, child) for child in command.commands])
    if getattr(command, "guild_only", False) and interaction.guild is None:
        return False
    for check in getattr(command, "checks", ()):
        try:
            result = check(interaction)
            if inspect.isawaitable(result):
                result = await result
        except Exception:
            return False
        if result is False:
            return False
    return True


async def command_autocomplete(interaction: discord.Interaction, current: str) -> list[app_commands.Choice[str]]:
    tree = getattr(interaction.client, "tree", None)
    if tree is None:
        return []
    lowered = current.lower()
    return [
        app_commands.Choice(name=name, value=name)
        for name, _command in findable_commands(tree)
        if lowered in name.lower()
    ][:MAX_CHOICES]


class HelpCog(commands.Cog):
    """``/help`` 与 ``/help <命令>``。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    @app_commands.command(
        name="help",
        description=localized("List every command, or explain one of them", "commands.help.description"),
    )
    @app_commands.describe(command=localized("Which command to explain in detail", "commands.help.param_command"))
    @app_commands.autocomplete(command=command_autocomplete)
    async def help(self, interaction: discord.Interaction, command: str | None = None) -> None:
        if command:
            await self._explain(interaction, command)
        else:
            await self._overview(interaction)

    async def _overview(self, interaction: discord.Interaction) -> None:
        t = self.bot.t
        locale = self.bot.i18n.resolve(interaction.locale)
        grouped: dict[str, list[str]] = {}

        for name, command in listable_commands(self.bot.tree):
            label = self._group_label(interaction, command)
            usable = await command_is_usable(interaction, command)
            payload = await command_payload(self.bot, command)
            description = localized_text(payload, "description", locale)
            prefix = "" if usable else f"{t(interaction, 'framework.help.unavailable')} "
            grouped.setdefault(label, []).append(f"{prefix}`/{name}` — {description}")

        total = sum(len(lines) for lines in grouped.values())
        embed = info_embed(
            t(interaction, "framework.help.title"),
            t(interaction, "framework.help.description", count=total),
        )
        for label, lines in self._ordered_groups(interaction, grouped):
            embed.add_field(name=label, value=_clip("\n".join(lines)), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    async def _explain(self, interaction: discord.Interaction, name: str) -> None:
        t = self.bot.t
        locale = self.bot.i18n.resolve(interaction.locale)
        target = self._find_command(name)
        if target is None:
            raise UserError("framework.help.unknown_command", command=name)

        payload = await command_payload(self.bot, target)
        lines = [localized_text(payload, "description", locale)]

        if isinstance(target, app_commands.Group):
            lines.append("")
            for child in target.commands:
                child_payload = await command_payload(self.bot, child)
                child_description = localized_text(child_payload, "description", locale)
                lines.append(f"• `/{target.name} {child.name}` — {child_description}")
        else:
            parameters = payload.get("options") or []
            if parameters:
                lines.append("")
                lines.append(t(interaction, "framework.help.parameters"))
                for parameter in parameters:
                    required = (
                        t(interaction, "framework.help.required")
                        if parameter.get("required")
                        else t(interaction, "framework.help.optional")
                    )
                    description = localized_text(parameter, "description", locale)
                    lines.append(f"• `{parameter['name']}`（{required}）— {description}")

        extras = self._aggregated_extras(target)
        permissions = extras.get("permissions") or ()
        if permissions:
            labels = ", ".join(t(interaction, f"permissions.{flag}") for flag in permissions)
            lines.append("")
            lines.append(t(interaction, "framework.help.requires_permissions", permissions=labels))
        if extras.get("owner_only"):
            lines.append("")
            lines.append(t(interaction, "framework.help.owner_only"))
        if not await command_is_usable(interaction, target):
            lines.append("")
            lines.append(t(interaction, "framework.help.not_usable"))

        embed = info_embed(f"/{target.qualified_name}", _clip("\n".join(lines)))
        await interaction.response.send_message(embed=embed, ephemeral=True)

    def _find_command(self, name: str) -> app_commands.Command | None:
        wanted = name.strip().lstrip("/").lower()
        candidates = findable_commands(self.bot.tree)
        for listed_name, command in candidates:
            if listed_name.lower() == wanted:
                return command
        for listed_name, command in candidates:
            if wanted in listed_name.lower():
                return command
        return None

    @staticmethod
    def _aggregated_extras(command: app_commands.Command) -> dict[str, Any]:
        """分组命令自己没有 extras，把子命令的合并起来供 /help 提示用。"""
        if not isinstance(command, app_commands.Group):
            return getattr(command, "extras", None) or {}
        children = list(command.commands)
        if not children:
            return {}
        permissions = sorted({flag for child in children for flag in (child.extras or {}).get("permissions") or ()})
        return {
            "permissions": tuple(permissions),
            "owner_only": all((child.extras or {}).get("owner_only") for child in children),
        }

    def _group_label(self, interaction: discord.Interaction, command: app_commands.Command) -> str:
        binding = getattr(command, "binding", None)
        if binding is not None:
            module_id = self.bot.loader.module_of_cog(type(binding).__name__)
            if module_id:
                return self.bot.t(interaction, f"modules.{module_id}.name")
        return self.bot.t(interaction, "framework.help.group_framework")

    def _ordered_groups(
        self, interaction: discord.Interaction, grouped: dict[str, list[str]]
    ) -> list[tuple[str, list[str]]]:
        """框架自己的命令排在最前，其余按模块名排序。"""
        framework_label = self.bot.t(interaction, "framework.help.group_framework")
        ordered: list[tuple[str, list[str]]] = []
        if framework_label in grouped:
            ordered.append((framework_label, grouped[framework_label]))
        ordered.extend((label, grouped[label]) for label in sorted(grouped) if label != framework_label)
        return ordered


def _clip(text: str, limit: int = FIELD_VALUE_LIMIT) -> str:
    if len(text) <= limit:
        return text
    return text[: limit - 1] + "…"
