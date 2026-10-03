"""moderation：处罚、警告累计与管理动作留痕。

这个模块是「写库 + 危险操作 + 权限校验 + mod-log 输出」这一类模块的样板。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="moderation",
    default_enabled=True,
    required_permissions=(
        "kick_members",
        "ban_members",
        "moderate_members",
        "manage_messages",
    ),
)


async def setup(bot: Any) -> None:
    from .cog import ModerationCog
    from .service import ModerationService

    service = ModerationService(
        bot.db,
        bot.guild_settings,
        logger=bot.log.getChild("moderation"),
    )
    await bot.add_cog(ModerationCog(bot, service))
