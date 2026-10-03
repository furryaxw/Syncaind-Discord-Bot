"""被依赖方：id 按字母序排在后半段，用来验证加载顺序不是靠字母序。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="zzz_provider")


class ZzzCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def setup(bot: Any) -> None:
    await bot.add_cog(ZzzCog(bot))
