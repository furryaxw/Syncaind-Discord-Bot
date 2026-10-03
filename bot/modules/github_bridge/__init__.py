"""github_bridge：GitHub ↔ Discord 账号映射。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="github_bridge",
    default_enabled=True,
    required_permissions=("manage_roles",),
)


async def setup(bot: Any) -> None:
    from bot.integrations.github import GitHubAccountStore

    from .cog import GitHubBridgeCog

    store = GitHubAccountStore(bot.db, logger=bot.log.getChild("github"))
    await bot.add_cog(GitHubBridgeCog(bot, store))
