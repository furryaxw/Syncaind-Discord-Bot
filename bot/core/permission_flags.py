"""权限位名字的解析：把用户敲的一串权限名变成 ``PermissionOverwrite``。

频道覆盖与角色权限都要用它，所以放框架层——两份必须保持同步的校验逻辑是最坏的选择。

刻意做成纯函数（不碰 Discord API），因为「用户敲错一个权限名」是最容易出错也最该测的地方。
"""

from __future__ import annotations

import re

import discord

# 权威来源是 discord.py 自己那张表，这里不重新维护一份清单。
VALID_FLAGS = frozenset(discord.Permissions.VALID_FLAGS)

# 逗号/空白/中英文标点都当分隔符——中文用户很容易敲出全角逗号和顿号。
_SPLIT = re.compile(r"[,\s，、]+")


class PermissionParseError(ValueError):
    """用户输入里含未知权限名。"""

    def __init__(self, unknown: tuple[str, ...]) -> None:
        super().__init__(f"未知权限名：{', '.join(unknown)}")
        self.unknown = unknown


def parse_flags(text: str | None) -> tuple[str, ...]:
    """把 ``"send_messages, view_channel"`` 解析成合法权限名元组（去重保序）。

    未知名字抛 :class:`PermissionParseError`，让调用方把它翻成用户可读的提示——
    静默忽略错别字是最坏的选择：用户会以为设置生效了。
    """
    if not text or not text.strip():
        return ()
    names = tuple(part for part in _SPLIT.split(text.strip()) if part)
    unknown = tuple(sorted({name for name in names if name not in VALID_FLAGS}))
    if unknown:
        raise PermissionParseError(unknown)
    seen: dict[str, None] = {}
    for name in names:
        seen.setdefault(name, None)
    return tuple(seen)


def build_overwrite(allow: tuple[str, ...], deny: tuple[str, ...]) -> discord.PermissionOverwrite:
    """构造权限覆盖。同一项同时出现在 allow 与 deny 时**以 deny 为准**（更保守）。"""
    overwrite = discord.PermissionOverwrite()
    for flag in allow:
        setattr(overwrite, flag, True)
    for flag in deny:
        setattr(overwrite, flag, False)
    return overwrite


def inherit_overwrite(*flags: str) -> discord.PermissionOverwrite:
    """把若干项恢复成「不设置」（即继承上层）——锁定/解锁用它。"""
    overwrite = discord.PermissionOverwrite()
    for flag in flags:
        setattr(overwrite, flag, None)
    return overwrite


def apply_overwrite(
    permissions: discord.Permissions, *, allow: tuple[str, ...], deny: tuple[str, ...]
) -> discord.Permissions:
    """在角色现有权限位的基础上允许/拒绝若干项。

    刻意不接受「整体覆盖」：``/role permissions`` 只改用户点名的几项，
    没提到的位保持原样——否则一次调用就会把角色的其它权限悄悄清掉。
    """
    updated = discord.Permissions(permissions.value)
    for flag in allow:
        setattr(updated, flag, True)
    for flag in deny:
        setattr(updated, flag, False)
    return updated
