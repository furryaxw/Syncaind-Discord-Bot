"""权限判定：角色层级比对。"""

from __future__ import annotations

from bot.core.checks import (
    ROLE_TOO_HIGH_FOR_ACTOR,
    ROLE_TOO_HIGH_FOR_BOT,
    can_manage_role,
    is_owner_id,
)
from tests.fakes import fake_member, fake_role

GUILD_OWNER_ID = 1


def test_owner_whitelist() -> None:
    assert is_owner_id(200, 200) is True
    assert is_owner_id(201, 200) is False


def test_managed_role_cannot_be_edited() -> None:
    verdict = can_manage_role(
        actor=fake_member(2, 70),
        role=fake_role(10, 10, managed=True),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == "errors.role_managed_by_integration"


def test_role_above_bot_cannot_be_edited() -> None:
    verdict = can_manage_role(
        actor=fake_member(2, 99),
        role=fake_role(10, 95),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == ROLE_TOO_HIGH_FOR_BOT


def test_role_above_actor_cannot_be_edited() -> None:
    verdict = can_manage_role(
        actor=fake_member(2, 60),
        role=fake_role(10, 70),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == ROLE_TOO_HIGH_FOR_ACTOR


def test_lower_role_can_be_edited() -> None:
    verdict = can_manage_role(
        actor=fake_member(2, 60),
        role=fake_role(10, 50),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict is None
