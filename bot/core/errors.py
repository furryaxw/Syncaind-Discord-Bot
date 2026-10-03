"""错误处理：把内部异常翻译成用户看得懂的一句话。"""

from __future__ import annotations

import logging
from typing import Any

import discord
from discord import app_commands

from .i18n import I18n


class UserError(Exception):
    """面向用户的错误。``key`` 是 i18n 键，``kwargs`` 用于填占位符。"""

    def __init__(self, key: str, **kwargs: Any) -> None:
        super().__init__(key)
        self.key = key
        self.kwargs = kwargs


def register_error_handler(
    tree: app_commands.CommandTree,
    *,
    i18n: I18n,
    logger: logging.Logger | None = None,
) -> None:
    """把统一的 app command 错误处理器挂到命令树上。"""
    log = logger or logging.getLogger("bot.errors")

    async def on_app_command_error(interaction: discord.Interaction, error: Exception) -> None:
        await handle_app_command_error(interaction, error, i18n=i18n, logger=log)

    tree.error(on_app_command_error)


async def handle_app_command_error(
    interaction: discord.Interaction,
    error: Exception,
    *,
    i18n: I18n,
    logger: logging.Logger,
) -> None:
    """把异常映射成文案并回给用户。刻意分开，方便离线测试这段映射逻辑。"""
    error = unwrap(error)
    key, kwargs, unexpected = _classify(error)
    if unexpected:
        logger.error(
            "处理命令 %s 时发生未预期异常",
            getattr(interaction, "command", None),
            exc_info=error,
        )

    title = i18n.t(interaction.locale, "errors.title")
    description = i18n.t(interaction.locale, key, **kwargs)
    embed = discord.Embed(title=title, description=description, color=0xED4245)

    try:
        if interaction.response.is_done():
            # 命令自己已经 defer 过（比如要先调外部接口）：只能走 followup，
            # 否则这里再 send_message 会「已经回应过」而丢掉整条提示。
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)
    except discord.HTTPException:
        logger.exception("回传错误提示失败")


def unwrap(error: Exception) -> Exception:
    """拆掉 discord.py 的包装，拿到真正的异常。

    ``app_commands`` 会把回调里抛出的任何异常包成 :class:`CommandInvokeError`
    （``.original`` 才是原始异常）。不拆这层，``UserError`` 就永远匹配不上：
    **面向用户的一句话会全部退化成「出错了」，并打一条带堆栈的 ERROR**。
    """
    seen = 0
    while isinstance(error, app_commands.CommandInvokeError) and getattr(error, "original", None) is not None:
        error = error.original
        seen += 1
        if seen > 5:  # 理论上到不了；防御性地避免构造出的自引用把这里转死
            break
    return error


def _classify(error: Exception) -> tuple[str, dict[str, Any], bool]:
    """返回 (i18n 键, 占位符, 是否是「不该发生」的异常)。

    ⚠️ 传进来的必须是 :func:`unwrap` 过的异常 —— ``CommandInvokeError`` 在这里永远匹配不上。
    """
    if isinstance(error, UserError):
        return error.key, dict(error.kwargs), False
    if isinstance(error, app_commands.MissingPermissions):
        return (
            "errors.missing_permissions",
            {"permissions": ", ".join(error.missing_permissions)},
            False,
        )
    if isinstance(error, app_commands.BotMissingPermissions):
        return (
            "errors.bot_missing_permissions",
            {"permissions": ", ".join(error.missing_permissions)},
            False,
        )
    if isinstance(error, app_commands.CommandOnCooldown):
        return "errors.cooldown", {"seconds": f"{error.retry_after:.0f}"}, False
    if isinstance(error, app_commands.CheckFailure):
        return "errors.permission_denied", {}, False
    if isinstance(error, discord.Forbidden):
        return "errors.discord_forbidden", {}, False
    if isinstance(error, discord.NotFound):
        return "errors.discord_not_found", {}, False
    if isinstance(error, discord.HTTPException):
        return "errors.discord_http", {"status": error.status}, False
    return "errors.unexpected", {}, True
