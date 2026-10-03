"""tools：只读的服务器/成员信息与权限诊断。

这个模块是「纯只读、不写库、无危险操作」这一类模块的样板。
"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="tools",
    default_enabled=True,
    required_permissions=(),
)


async def setup(bot: Any) -> None:
    from .cog import ToolsCog

    await bot.add_cog(ToolsCog(bot))
