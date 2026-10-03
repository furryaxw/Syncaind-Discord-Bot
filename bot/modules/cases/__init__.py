"""cases：处罚记录的查询与撤销。

这个模块只做「读已有记录」和「撤销」，本身不产生新的处罚——处罚由 moderation 写入。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="cases",
    default_enabled=True,
    required_permissions=("moderate_members",),
    # 记录表由 moderation 写入，但没有它就没东西可查。
    depends_on=("moderation",),
)


async def setup(bot: Any) -> None:
    from .cog import CasesCog
    from .service import CaseService

    service = CaseService(bot.db, logger=bot.log.getChild("cases"))
    await bot.add_cog(CasesCog(bot, service))
