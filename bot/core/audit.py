"""管理动作的留痕：统一的审计日志 reason。"""

from __future__ import annotations

import discord


def audit_reason(interaction: discord.Interaction, reason: str | None = None) -> str:
    """写进 Discord 自己那份审计日志的 reason 字符串。

    Discord 的 reason 是给管理员在审计日志里看的，所以必须带上执行者——
    否则一条「刷屏」根本认不出是谁下的手。
    """
    actor = f"{interaction.user} ({interaction.user.id})"
    return f"{reason} | {actor}" if reason else actor
