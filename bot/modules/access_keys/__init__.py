"""access_keys：本地 key 池。

池、key、活动、入口卡片、领取与补发都在机器人自己的数据库里；`/key` 面板负责导入与操作。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="access_keys",
    default_enabled=True,
    required_permissions=("manage_guild",),
)


async def setup(bot: Any) -> None:
    from bot.integrations.keypool import KeyPoolStore

    from .cog import AccessKeysCog
    from .pool_service import KeyPoolService

    logger = bot.log.getChild("access_keys")
    service = KeyPoolService(KeyPoolStore(bot.db, logger=logger), logger=logger)
    cog = AccessKeysCog(bot, service)
    await bot.add_cog(cog)
    cog.start_sweeper()
