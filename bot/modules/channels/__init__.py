"""channels：频道管理（建、删、改名、锁定、慢速模式、权限覆盖）。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="channels",
    default_enabled=True,
    required_permissions=("manage_channels", "manage_roles"),
)


async def setup(bot: Any) -> None:
    from .cog import ChannelsCog

    await bot.add_cog(ChannelsCog(bot))
