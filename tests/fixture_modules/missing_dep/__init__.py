"""依赖一个不存在的模块。"""

from __future__ import annotations

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="missing_dep", depends_on=("does_not_exist",))
