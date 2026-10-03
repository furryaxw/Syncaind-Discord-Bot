"""setup() 里抛异常的模块。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="setup_fails")


class HalfRegisteredCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def setup(bot: Any) -> None:
    await bot.add_cog(HalfRegisteredCog(bot))
    raise RuntimeError("setup 炸了")
