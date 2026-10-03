"""默认停用的模块。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="off_by_default", default_enabled=False)


class OffCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def setup(bot: Any) -> None:
    await bot.add_cog(OffCog(bot))
