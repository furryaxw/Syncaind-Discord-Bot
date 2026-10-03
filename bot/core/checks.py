"""权限判定：角色层级比对与 owner 白名单。

这里全部是**纯函数**，只依赖传入对象上的少量属性，因此可以离线测试。
返回 ``None`` 表示放行，返回字符串表示拒绝，字符串是该原因的 i18n 键。
"""

from __future__ import annotations

from typing import Any

TARGET_IS_OWNER = "errors.hierarchy.target_is_owner"
TARGET_IS_SELF = "errors.hierarchy.target_is_self"
TARGET_IS_BOT = "errors.hierarchy.target_is_bot"
BOT_ROLE_TOO_LOW = "errors.hierarchy.bot_role_too_low"
ACTOR_ROLE_TOO_LOW = "errors.hierarchy.actor_role_too_low"
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


def can_act_on(
    *,
    actor: Any,
    target: Any,
    bot_member: Any,
    guild_owner_id: int,
) -> str | None:
    """判断 ``actor`` 能否对 ``target`` 执行处罚类操作。

    只覆盖 Discord 自己会强制执行的规则——这些规则不因为调用者是机器人 owner 而放宽，
    放宽只会换来一次 API 403。
    """
    if getattr(target, "id", None) == guild_owner_id:
        return TARGET_IS_OWNER
    if getattr(target, "id", None) == getattr(actor, "id", None):
        return TARGET_IS_SELF
    if getattr(target, "id", None) == getattr(bot_member, "id", None):
        return TARGET_IS_BOT
    if position_of(bot_member) <= position_of(target):
        return BOT_ROLE_TOO_LOW
    if getattr(actor, "id", None) != guild_owner_id and position_of(actor) <= position_of(target):
        return ACTOR_ROLE_TOO_LOW
    return None


def can_manage_role(
    *,
    actor: Any,
    role: Any,
    bot_member: Any,
    guild_owner_id: int,
) -> str | None:
    """判断 ``actor`` 能否创建/修改/删除 ``role``。"""
    if getattr(role, "managed", False):
        return "errors.role_managed_by_integration"
    if getattr(role, "is_default", lambda: False)():
        return "errors.role_is_default"
    if position_of(bot_member) <= position_of(role):
        return ROLE_TOO_HIGH_FOR_BOT
    if getattr(actor, "id", None) != guild_owner_id and position_of(actor) <= position_of(role):
        return ROLE_TOO_HIGH_FOR_ACTOR
    return None
