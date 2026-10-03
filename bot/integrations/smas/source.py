"""从 access server 读「某个 GitHub 用户的有效权限节点」。

上层（同步引擎）只依赖 :class:`AccessSource`，所以它能用假实现完全离线测；
真实实现 :class:`AccessServerSource` 建在 :class:`AccessClient` 上。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

from .client import SYSTEM_TEAM_ID, AccessClient, AccessError


class AccessSourceError(RuntimeError):
    """读取失败。

    ⚠️ 上层必须把「读失败」和「他没有任何节点」**严格区分**：前者绝不能当成后者处理，
    否则一次网络抖动就会把所有角色的撤掉。
    """

    def __init__(self, message: str, *, status: int | None = None) -> None:
        super().__init__(message)
        self.status = status


@dataclass(frozen=True)
class EffectiveNodes:
    """一个人在某次读取里的有效节点，外加出处（便于排查「他这个角色哪来的」）。"""

    github_user_id: str
    nodes: frozenset[str] = field(default_factory=frozenset)
    sources: tuple[str, ...] = ()


class AccessSource(Protocol):
    async def effective_nodes(self, github_user_id: str) -> EffectiveNodes:
        """读某个 GitHub 用户的有效节点。**失败必须抛异常，不要返回空集合。**"""
        ...


class AccessServerSource:
    """真实实现：``system.users.permissions`` 的 ``read``。

    契约要点（已复核）：
    * 必须持 ``system.users.read`` **且带 System Team 上下文**；
    * 返回的 ``permissions`` 是**系统作用域**节点（``team_id IS NULL`` ∪ System Team 模板展开），
      ``assignments`` 是**跨全部 Team** 的授权记录 —— 两边合起来才是「他的有效节点」；
    * ``user_id`` 是等值匹配，不会因子串而误命中。

    刻意**不**在这里再调 ``team.<id>.permission_assignments``：那是逐 Team 的权威明细，
    用于排查「为什么他该有这个角色」，日常同步一次调用就够了。
    """

    def __init__(self, client: AccessClient, *, logger: Any | None = None) -> None:
        self._client = client
        self._logger = logger

    async def effective_nodes(self, github_user_id: str) -> EffectiveNodes:
        try:
            payload = await self._client.call(
                "read",
                "system.users.permissions",
                {"user_id": github_user_id},
                team_id=SYSTEM_TEAM_ID,
            )
        except AccessError as exc:
            # 关键：失败就抛，绝不当成「他没有权限」。
            raise AccessSourceError(f"读 {github_user_id} 的权限失败：{exc.code}") from exc

        nodes: set[str] = set()
        sources: list[str] = []
        for item in payload.get("permissions") or []:
            if isinstance(item, dict) and item.get("value"):
                nodes.add(str(item["value"]))
                if item.get("source_id"):
                    sources.append(str(item["source_id"]))
        for item in payload.get("assignments") or []:
            if isinstance(item, dict) and item.get("node"):
                nodes.add(str(item["node"]))
                if item.get("source_id"):
                    sources.append(str(item["source_id"]))
        return EffectiveNodes(
            github_user_id=github_user_id,
            nodes=frozenset(nodes),
            sources=tuple(dict.fromkeys(sources)),
        )


class UnconfiguredAccessSource:
    """没配 access server 时的占位：任何读取都明确失败，而不是假装「他没权限」。"""

    async def effective_nodes(self, github_user_id: str) -> EffectiveNodes:
        raise AccessSourceError("还没有配置 access server 连接（服务号地址与密钥）")
