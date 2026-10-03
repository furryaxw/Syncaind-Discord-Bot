"""access_keys：按批次发激活码（抽奖 / 先到先得，都从按钮进）。

**依赖 `github_bridge`**：SMAS 相关操作都要求先 `/link github`，
所以这个模块要能读到那张 GitHub ↔ Discord 映射表；`github_bridge` 被停用时它也不该假装能用。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="access_keys",
    default_enabled=True,
    depends_on=("github_bridge",),
    # 开一次发码活动是服务器配置动作；参与（点按钮）不需要服务器权限。
    required_permissions=("manage_guild",),
)


async def setup(bot: Any) -> None:
    import aiohttp

    from bot.integrations.github import GitHubAccountStore
    from bot.integrations.smas import AccessClient
    from bot.integrations.smas.key_store import (
        KeyDeliveryStore,
        KeyDeniedRoleStore,
        KeyDropStore,
    )

    from .cog import AccessKeysCog, AccessKeysView

    settings = bot.settings
    client: Any | None = None
    if settings.access_base_url and settings.access_service_secret:
        client = AccessClient(
            settings.access_base_url,
            service_id=settings.access_service_id,
            service_secret=settings.access_service_secret,
            session=aiohttp.ClientSession(),
            logger=bot.log.getChild("access_keys"),
        )

    logger = bot.log.getChild("access_keys")
    cog = AccessKeysCog(
        bot,
        KeyDeliveryStore(bot.db, logger=logger),
        KeyDropStore(bot.db, logger=logger),
        GitHubAccountStore(bot.db, logger=logger),
        KeyDeniedRoleStore(bot.db, logger=logger),
        client=client,
    )
    await bot.add_cog(cog)
    # 持久化按钮：注册一次，旧消息上的按钮在重启后照样能点
    # （回调里的 drop 状态按消息 id 反查，所以不需要每个活动各注册一个 view）。
    bot.add_view(AccessKeysView(cog))
    cog.start_sweeping()
