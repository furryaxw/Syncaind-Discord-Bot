"""github_feed：把 GitHub 的 release notes 推到指定的帖子或频道。

一个仓库绑一个目标（帖子或频道都在 Discord API 里算 channel），绑定时会**把已有 release
按时间顺序回填**，然后游标停在新处；之后 `sync` 只推游标之后的。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="github_feed",
    default_enabled=True,
    required_permissions=("manage_guild",),
)


async def setup(bot: Any) -> None:
    from bot.integrations.github import ReleaseTargetStore, WatcherCursorStore

    from .cog import GitHubFeedCog

    logger = bot.log.getChild("github_feed")
    cog = GitHubFeedCog(
        bot,
        ReleaseTargetStore(bot.db, logger=logger),
        cursor=WatcherCursorStore(bot.db, logger=logger),
    )
    await bot.add_cog(cog)
    # 配了监听服务就起常驻订阅；没配就只靠 /feed sync。
    cog.start_watching()
