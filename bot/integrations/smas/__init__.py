"""SMAS 集成。

这一层只认识**它自己的概念**（权限节点、服务号、RPC 信封），不认识 Discord；
所以节点匹配与「该有哪些角色」的判定都是纯函数，可以完全离线测。
"""

from .client import SYSTEM_TEAM_ID, AccessClient, AccessError, AccessSession, websocket_url
from .nodes import (
    NodePatternError,
    desired_role_ids,
    is_valid_pattern,
    matches,
    normalize_pattern,
)
from .source import (
    AccessServerSource,
    AccessSource,
    AccessSourceError,
    EffectiveNodes,
    UnconfiguredAccessSource,
)
from .store import NodeRoleBinding, NodeRoleBindingStore

__all__ = [
    "SYSTEM_TEAM_ID",
    "AccessClient",
    "AccessError",
    "AccessServerSource",
    "AccessSession",
    "AccessSource",
    "AccessSourceError",
    "EffectiveNodes",
    "NodePatternError",
    "NodeRoleBinding",
    "NodeRoleBindingStore",
    "UnconfiguredAccessSource",
    "desired_role_ids",
    "is_valid_pattern",
    "matches",
    "normalize_pattern",
    "websocket_url",
]
