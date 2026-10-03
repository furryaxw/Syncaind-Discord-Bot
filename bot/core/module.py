"""模块契约：模块向框架声明的元数据，以及框架记录的状态。

模块包必须在 ``__init__.py`` 里暴露 ``MODULE_META`` 与 ``async def setup(bot)``。
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum


class ModuleStatus(str, Enum):
    """模块在运行时的状态。"""

    LOADED = "loaded"
    DISABLED = "disabled"
    FAILED = "failed"
    DEPENDENCY_MISSING = "dependency_missing"


@dataclass(frozen=True)
class ModuleMeta:
    """模块自述。名称与描述是 i18n 键，默认按模块 id 推导。"""

    id: str
    name_key: str = ""
    description_key: str = ""
    default_enabled: bool = True
    required_permissions: tuple[str, ...] = ()
    depends_on: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.id or not self.id.replace("_", "").isalnum():
            raise ValueError(f"模块 id 非法：{self.id!r}（只允许字母、数字与下划线）")
        if not self.name_key:
            object.__setattr__(self, "name_key", f"modules.{self.id}.name")
        if not self.description_key:
            object.__setattr__(self, "description_key", f"modules.{self.id}.description")


@dataclass
class ModuleInfo:
    """框架侧记录的模块状态。``error`` 只在非 LOADED 时出现。"""

    meta: ModuleMeta
    status: ModuleStatus
    error: str | None = None

    @property
    def id(self) -> str:
        return self.meta.id
