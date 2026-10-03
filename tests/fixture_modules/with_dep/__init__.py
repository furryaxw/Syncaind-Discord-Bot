"""依赖 good 的模块。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="with_dep", depends_on=("good",))


class WithDepCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def setup(bot: Any) -> None:
    await bot.add_cog(WithDepCog(bot))
