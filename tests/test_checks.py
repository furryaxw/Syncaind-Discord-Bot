"""权限判定：角色层级比对与角色可改性。"""

from __future__ import annotations

from types import SimpleNamespace

from bot.core.checks import (
    ACTOR_ROLE_TOO_LOW,
    BOT_ROLE_TOO_LOW,
    ROLE_TOO_HIGH_FOR_ACTOR,
    ROLE_TOO_HIGH_FOR_BOT,
    TARGET_IS_BOT,
    TARGET_IS_OWNER,
    TARGET_IS_SELF,
    can_act_on,
    can_manage_role,
    is_owner_id,
)
from tests.fakes import fake_member, fake_role

GUILD_OWNER_ID = 1


def test_owner_whitelist() -> None:
    assert is_owner_id(200, 200) is True
    assert is_owner_id(201, 200) is False


def test_guild_owner_cannot_be_punished() -> None:
    verdict = can_act_on(
        actor=fake_member(2, 90),
        target=fake_member(GUILD_OWNER_ID, 100),
        bot_member=fake_member(999, 95),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == TARGET_IS_OWNER


def test_self_punishment_is_rejected() -> None:
    actor = fake_member(2, 50)
    verdict = can_act_on(
        actor=actor,
        target=actor,
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == TARGET_IS_SELF


def test_punishing_the_bot_itself_is_rejected() -> None:
    bot_member = fake_member(999, 90)
    verdict = can_act_on(
        actor=fake_member(2, 95),
        target=bot_member,
        bot_member=bot_member,
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == TARGET_IS_BOT


def test_bot_role_below_target_is_rejected() -> None:
    verdict = can_act_on(
        actor=fake_member(2, 99),
        target=fake_member(3, 80),
        bot_member=fake_member(999, 80),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == BOT_ROLE_TOO_LOW


def test_actor_role_below_target_is_rejected() -> None:
    verdict = can_act_on(
        actor=fake_member(2, 50),
        target=fake_member(3, 60),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict == ACTOR_ROLE_TOO_LOW


def test_higher_actor_is_allowed() -> None:
    verdict = can_act_on(
        actor=fake_member(2, 70),
        target=fake_member(3, 60),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict is None


def test_guild_owner_bypasses_the_actor_role_check() -> None:
    verdict = can_act_on(
        actor=fake_member(GUILD_OWNER_ID, 1),
        target=fake_member(3, 60),
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict is None


def test_member_without_top_role_is_treated_as_everyone() -> None:
    bare = SimpleNamespace(id=3)
    verdict = can_act_on(
        actor=fake_member(2, 10),
        target=bare,
        bot_member=fake_member(999, 90),
        guild_owner_id=GUILD_OWNER_ID,
    )

    assert verdict is None


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
