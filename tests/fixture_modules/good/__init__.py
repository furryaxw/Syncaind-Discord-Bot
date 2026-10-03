"""加载器测试用的正常模块。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(id="good", required_permissions=("manage_guild",))


class GoodCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot


async def setup(bot: Any) -> None:
    await bot.add_cog(GoodCog(bot))
