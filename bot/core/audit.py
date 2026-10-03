"""管理动作的留痕：统一的 mod-log 频道解析与投递。

处罚（moderation）与撤销（cases）都要留痕，所以这段逻辑放框架里，
模块只管自己那张 Embed 长什么样。
"""

from __future__ import annotations

import logging
from typing import Any

import discord

LOGGER = logging.getLogger("bot.audit")


def audit_reason(interaction: discord.Interaction, reason: str | None = None) -> str:
    """写进 Discord 自己那份审计日志的 reason 字符串。

    Discord 的 reason 是给管理员在审计日志里看的，所以必须带上执行者——
    否则一条「刷屏」根本认不出是谁下的手。
    """
    actor = f"{interaction.user} ({interaction.user.id})"
    return f"{reason} | {actor}" if reason else actor


async def resolve_mod_log_channel(bot: Any, guild: discord.Guild) -> discord.abc.Messageable | None:
    """解析该服务器的 mod-log 频道。没配置或不可用就返回 ``None``（不抛异常）。"""
    settings = await bot.guild_settings.get(guild.id)
    channel_id = settings.mod_log_channel_id
    if channel_id is None:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None or not isinstance(channel, discord.abc.Messageable):
        LOGGER.warning("mod-log 频道 %s 不存在或不可发送消息", channel_id)
        return None
    return channel


async def post_to_mod_log(bot: Any, guild: discord.Guild, embed: discord.Embed) -> bool:
    """把一条留痕投到 mod-log。返回是否真的发出去了。"""
    channel = await resolve_mod_log_channel(bot, guild)
    if channel is None:
        return False
    try:
        await channel.send(embed=embed)
    except discord.HTTPException:
        LOGGER.exception("写入 mod-log 失败：guild=%s", guild.id)
        return False
    return True
