"""access_roles：把 SMAS 的权限节点同步成 Discord 角色。

**依赖 `github_bridge`**：SMAS 用 GitHub 数字 id 标识人，所以同步的第一步是
「这个 Discord 用户对应哪个 GitHub 账号」——那张映射表就是桥梁；`github_bridge`
被停用时它也不该假装能用。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="access_roles",
    default_enabled=True,
    depends_on=("github_bridge",),
    required_permissions=("manage_roles",),
)


async def setup(bot: Any) -> None:
    import aiohttp

    from bot.integrations.github import GitHubAccountStore
    from bot.integrations.smas import AccessClient, AccessServerSource, NodeRoleBindingStore

    from .cog import AccessRolesCog

    logger = bot.log.getChild("access_roles")
    settings = bot.settings
    source: Any | None = None
    if settings.access_base_url and settings.access_service_secret:
        source = AccessServerSource(
            AccessClient(
                settings.access_base_url,
                service_id=settings.access_service_id,
                service_secret=settings.access_service_secret,
                session=aiohttp.ClientSession(),
                logger=logger,
            ),
            logger=logger,
        )

    await bot.add_cog(
        AccessRolesCog(
            bot,
            NodeRoleBindingStore(bot.db, logger=logger),
            GitHubAccountStore(bot.db, logger=logger),
            source=source,
        )
    )
