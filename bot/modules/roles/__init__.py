"""roles：角色管理（建、删、改名、改色、权限位、批量授予与移除）。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="roles",
    default_enabled=True,
    required_permissions=("manage_roles",),
)


async def setup(bot: Any) -> None:
    from .cog import RolesCog

    await bot.add_cog(RolesCog(bot))
