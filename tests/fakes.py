"""测试替身：够用的假对象，避免为了跑测试去连 Discord。"""

from __future__ import annotations

import logging
from types import SimpleNamespace
from typing import Any


class FakeBot:
    """只实现模块加载器用到的那几个接口。"""

    def __init__(self) -> None:
        self.cogs: dict[str, Any] = {}
        self.log = logging.getLogger("test.fake_bot")

    async def add_cog(self, cog: Any) -> None:
        self.cogs[type(cog).__name__] = cog

    async def remove_cog(self, name: str) -> None:
        self.cogs.pop(name, None)


def fake_member(member_id: int, position: int, *, name: str = "member") -> SimpleNamespace:
    """角色层级比对只读 ``id`` 与 ``top_role.position``。"""
    return SimpleNamespace(
        id=member_id,
        name=name,
        display_name=name,
        mention=f"<@{member_id}>",
        top_role=SimpleNamespace(position=position),
    )


def fake_role(role_id: int, position: int, *, name: str = "role", managed: bool = False) -> SimpleNamespace:
    return SimpleNamespace(
        id=role_id,
        name=name,
        mention=f"<@&{role_id}>",
        position=position,
        managed=managed,
        is_default=lambda: False,
    )
