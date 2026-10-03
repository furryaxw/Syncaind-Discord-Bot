"""reaction_roles：点表情自动给/收角色。

映射落库，事件用 raw —— 重启后照样工作，这也是它必须写数据库的原因。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="reaction_roles",
    default_enabled=True,
    required_permissions=("manage_roles",),
)


async def setup(bot: Any) -> None:
    from .cog import ReactionRolesCog
    from .service import ReactionRoleService

    service = ReactionRoleService(bot.db, logger=bot.log.getChild("reaction_roles"))
    await bot.add_cog(ReactionRolesCog(bot, service))
