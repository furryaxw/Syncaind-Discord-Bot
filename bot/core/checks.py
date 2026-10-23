"""权限判定：角色层级比对与 owner 白名单。

这里全部是**纯函数**，只依赖传入对象上的少量属性，因此可以离线测试。
返回 ``None`` 表示放行，返回字符串表示拒绝，字符串是该原因的 i18n 键。
"""

from __future__ import annotations

from typing import Any

ROLE_TOO_HIGH_FOR_BOT = "errors.hierarchy.role_above_bot"
ROLE_TOO_HIGH_FOR_ACTOR = "errors.hierarchy.role_above_actor"


def is_owner_id(user_id: int, owner_id: int) -> bool:
    """owner 白名单判定（只用于框架级命令）。"""
    return user_id == owner_id


def position_of(entity: Any) -> int:
    """排序位置：成员取其最高角色，角色取其自身。取不到时按 0（等同 @everyone）处理。"""
    role = getattr(entity, "top_role", entity)
    position = getattr(role, "position", 0)
    try:
        return int(position)
    except (TypeError, ValueError):
        return 0


def can_manage_role(
    *,
    actor: Any,
    role: Any,
    bot_member: Any,
    guild_owner_id: int,
) -> str | None:
    """判断 ``actor`` 能否操作比它低的 ``role``。"""
    if getattr(role, "managed", False):
        return "errors.role_managed_by_integration"
    if getattr(role, "is_default", lambda: False)():
        return "errors.role_is_default"
    if position_of(bot_member) <= position_of(role):
        return ROLE_TOO_HIGH_FOR_BOT
    if getattr(actor, "id", None) != guild_owner_id and position_of(actor) <= position_of(role):
        return ROLE_TOO_HIGH_FOR_ACTOR
    return None
