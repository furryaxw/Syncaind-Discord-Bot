"""跨模块服务注册表。

模块之间不直接 import 彼此的内部实现；提供能力的模块把自己的服务注册到这里，
使用方按名字取。模块被停用/重载时注销，使用方下次调用会立刻发现服务不存在。
"""

from __future__ import annotations

from typing import Any


class ServiceRegistry:
    """一个极小的服务表。

    * ``register`` 重复注册同名服务会抛错，避免两个模块悄悄覆盖对方。
    * ``get`` 返回 ``None``，调用方据此降级；``require`` 在缺失时抛错。
    """

    def __init__(self) -> None:
        self._services: dict[str, Any] = {}

    def register(self, name: str, service: Any) -> None:
        if name in self._services:
            raise ValueError(f"服务已注册：{name}")
        self._services[name] = service

    def unregister(self, name: str) -> None:
        self._services.pop(name, None)

    def get(self, name: str) -> Any | None:
        return self._services.get(name)

    def require(self, name: str) -> Any:
        service = self.get(name)
        if service is None:
            raise LookupError(f"服务不可用：{name}")
        return service

    def has(self, name: str) -> bool:
        return name in self._services

    def names(self) -> tuple[str, ...]:
        return tuple(sorted(self._services))
