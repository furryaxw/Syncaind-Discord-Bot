"""框架级命令：/module list | enable | disable | reload（仅 owner）。

这些命令由框架自己提供，不属于任何业务模块——否则「所有模块都坏了」时就没有补救手段。
"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from .checks import is_owner_id
from .errors import UserError
from .module import ModuleStatus
from .translator import localized
from .ui import add_fields, error_embed, info_embed, ok_embed

LOGGER = logging.getLogger("bot.framework")

# 写进命令的 extras，供 /help 展示「这条只有 owner 能用」。extras 不会进 Discord payload。
OWNER_ONLY: dict[str, Any] = {"owner_only": True}

STATUS_LABEL_KEY = {
    ModuleStatus.LOADED: "framework.module.status_loaded",
    ModuleStatus.DISABLED: "framework.module.status_disabled",
    ModuleStatus.FAILED: "framework.module.status_failed",
    ModuleStatus.DEPENDENCY_MISSING: "framework.module.status_dependency_missing",
}


async def owner_only(interaction: discord.Interaction) -> bool:
    """框架级命令只允许 OWNER_ID 使用。"""
    settings = getattr(interaction.client, "settings", None)
    owner_id = getattr(settings, "owner_id", None)
    if owner_id is None or not is_owner_id(interaction.user.id, owner_id):
        raise UserError("errors.owner_only")
    return True


async def module_autocomplete(
    interaction: discord.Interaction,
    current: str,
) -> list[app_commands.Choice[str]]:
    loader = getattr(interaction.client, "loader", None)
    if loader is None:
        return []
    lowered = current.lower()
    return [app_commands.Choice(name=info.id, value=info.id) for info in loader.infos() if lowered in info.id.lower()][
        :25
    ]


class FrameworkCog(commands.Cog):
    """模块管理命令。"""

    def __init__(self, bot: Any) -> None:
        self.bot = bot

    module_group = app_commands.Group(
        name="module",
        description=localized("Manage bot modules", "commands.module.description"),
    )

    @module_group.command(
        name="list",
        description=localized("List every module and its status", "commands.module.list.description"),
        extras=OWNER_ONLY,
    )
    @app_commands.check(owner_only)
    async def module_list(self, interaction: discord.Interaction) -> None:
        infos = self.bot.loader.infos()
        if not infos:
            await interaction.response.send_message(
                embed=info_embed(
                    self.bot.t(interaction, "framework.module.list_title"),
                    self.bot.t(interaction, "framework.module.no_modules"),
                ),
                ephemeral=True,
            )
            return

        embed = info_embed(
            self.bot.t(interaction, "framework.module.list_title"),
            self.bot.t(interaction, "framework.module.list_description", count=len(infos)),
        )
        for info in infos:
            status = self.bot.t(interaction, STATUS_LABEL_KEY[info.status])
            lines = [
                self.bot.t(interaction, info.meta.name_key),
                status,
            ]
            if info.error:
                lines.append(self.bot.t(interaction, "framework.module.error_line", error=info.error))
            if info.meta.depends_on:
                lines.append(
                    self.bot.t(
                        interaction,
                        "framework.module.depends_line",
                        dependencies=", ".join(info.meta.depends_on),
                    )
                )
            embed.add_field(name=f"`{info.id}`", value="\n".join(lines), inline=False)
        await interaction.response.send_message(embed=embed, ephemeral=True)

    @module_group.command(
        name="enable",
        description=localized("Enable a module", "commands.module.enable.description"),
        extras=OWNER_ONLY,
    )
    @app_commands.autocomplete(module=module_autocomplete)
    @app_commands.describe(module=localized("Module id", "commands.module.param_module"))
    @app_commands.check(owner_only)
    async def module_enable(self, interaction: discord.Interaction, module: str) -> None:
        await self._set_enabled(interaction, module, enabled=True)

    @module_group.command(
        name="disable",
        description=localized("Disable a module", "commands.module.disable.description"),
        extras=OWNER_ONLY,
    )
    @app_commands.autocomplete(module=module_autocomplete)
    @app_commands.describe(module=localized("Module id", "commands.module.param_module"))
    @app_commands.check(owner_only)
    async def module_disable(self, interaction: discord.Interaction, module: str) -> None:
        await self._set_enabled(interaction, module, enabled=False)

    @module_group.command(
        name="reload",
        description=localized(
            "Reload a module's code without restarting the bot",
            "commands.module.reload.description",
        ),
        extras=OWNER_ONLY,
    )
    @app_commands.autocomplete(module=module_autocomplete)
    @app_commands.describe(module=localized("Module id", "commands.module.param_module"))
    @app_commands.check(owner_only)
    async def module_reload(self, interaction: discord.Interaction, module: str) -> None:
        loader = self.bot.loader
        if loader.info(module) is None:
            raise UserError("framework.module.unknown", module=module)

        await interaction.response.defer(ephemeral=True)
        info = await loader.reload(module)
        # 模块自带的文案也要跟着重读，否则改了 locales/ 得重启才生效。
        locales_reloaded = self.bot.i18n.reload_if_possible()
        await self.bot.sync_commands()

        lines: list[str] = []
        if info.status is ModuleStatus.LOADED:
            embed = ok_embed(
                self.bot.t(interaction, "framework.module.reloaded"),
                self.bot.t(interaction, info.meta.name_key),
            )
        else:
            embed = error_embed(
                self.bot.t(interaction, "framework.module.reload_failed"),
                self.bot.t(interaction, STATUS_LABEL_KEY[info.status]) + (f"\n`{info.error}`" if info.error else ""),
            )
        if not locales_reloaded:
            lines.append(self.bot.t(interaction, "framework.module.locale_reload_failed"))
        if lines:
            embed = add_fields(embed, [(self.bot.t(interaction, "framework.module.notes"), "\n".join(lines), False)])
        await interaction.edit_original_response(embed=embed)

    async def _set_enabled(
        self,
        interaction: discord.Interaction,
        module: str,
        *,
        enabled: bool,
    ) -> None:
        loader = self.bot.loader
        if loader.info(module) is None:
            raise UserError("framework.module.unknown", module=module)

        await interaction.response.defer(ephemeral=True)
        info = await loader.set_enabled(module, enabled)
        await self.bot.sync_commands()

        embed = ok_embed(
            self.bot.t(
                interaction,
                "framework.module.enabled" if enabled else "framework.module.disabled",
                name=self.bot.t(interaction, info.meta.name_key),
            ),
            self.bot.t(interaction, STATUS_LABEL_KEY[info.status]) + (f"\n`{info.error}`" if info.error else ""),
        )
        embed = add_fields(
            embed,
            [
                (
                    self.bot.t(interaction, "framework.module.required_permissions"),
                    ", ".join(info.meta.required_permissions) or None,
                    False,
                )
            ],
        )
        await interaction.edit_original_response(embed=embed)
