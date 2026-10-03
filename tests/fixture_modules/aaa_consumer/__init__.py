"""依赖方：id 按字母序排在前面，但依赖 zzz_provider。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="aaa_consumer", depends_on=("zzz_provider",))


class AaaCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def setup(bot: Any) -> None:
    await bot.add_cog(AaaCog(bot))
