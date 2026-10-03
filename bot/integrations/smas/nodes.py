"""权限节点的匹配，与「这个该有哪些角色」的判定。**纯函数，不碰 Discord。**

节点的形状（来自 access server 的权限目录）：

* ``system.users.read`` / ``system.permissions.read``
* ``team.<team_id>.packages.read``、``team.<team_id>.package_uploads.create``
* ``team.<team_id>.<package_id>.manage`` —— **包级节点是动态的**，所以绑定必须支持模式
* 模板也是节点：服务端求值时把模板节点展开成成员节点

**通配只允许出现在末段**（和服务端自己的语义一致）：``team.acme.*`` 覆盖该 Team 下的所有节点，
``team.*`` 覆盖所有 Team。不允许 ``team.*.read`` 这种中间通配 —— 那会让人以为匹配的是任意一级，
而服务端的节点语义里没有这个能力。
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Sequence

# 节点段：小写字母、数字、下划线、连字符（team id 由名字派生，所以会有连字符）
SEGMENT = re.compile(r"^[a-z0-9_-]+$")
WILDCARD = "*"


class NodePatternError(ValueError):
    """节点模式不合法。"""


def normalize_pattern(pattern: str) -> str:
    """规范化并校验一个节点模式。不合法就抛 :class:`NodePatternError`。"""
    value = pattern.strip().lower()
    if not value:
        raise NodePatternError("节点模式不能为空")

    segments = value.split(".")
    for index, segment in enumerate(segments):
        if segment == WILDCARD:
            if index != len(segments) - 1:
                raise NodePatternError(f"通配只能出现在末段：{pattern}")
            continue
        if not SEGMENT.match(segment):
            raise NodePatternError(f"节点段不合法：{segment}")
    if value.count(WILDCARD) > 1:
        raise NodePatternError(f"最多一个通配：{pattern}")
    return value


def is_valid_pattern(pattern: str) -> bool:
    try:
        normalize_pattern(pattern)
    except NodePatternError:
        return False
    return True


def matches(pattern: str, node: str) -> bool:
    """``node`` 是否被 ``pattern`` 覆盖。两边都按小写比较。"""
    normalized = pattern.strip().lower()
    candidate = node.strip().lower()
    if not normalized or not candidate:
        return False
    if not normalized.endswith(f".{WILDCARD}"):
        return normalized == candidate
    prefix = normalized[: -len(WILDCARD)]  # 保留末尾的点，如 'team.acme.'
    return candidate.startswith(prefix) and len(candidate) > len(prefix)


def desired_role_ids(
    nodes: Iterable[str],
    bindings: Sequence[tuple[str, int]],
) -> set[int]:
    """给定一个人的有效节点与「模式 → 角色」绑定表，算出他**该**有哪些角色。

    只返回绑定表里出现过的角色 —— 这是这个功能最重要的一条安全边界：
    **机器人只会动你亲手绑过的角色，绝不碰别的。**
    """
    effective = [node for node in nodes if node]
    return {role_id for pattern, role_id in bindings if any(matches(pattern, node) for node in effective)}
