"""moderation 的数据部分：case 编号、警告累计、升级判定。"""

from __future__ import annotations

import pytest

from bot.core.database import Database
from bot.core.store import GuildSettingsStore
from bot.modules.moderation.service import ModerationService, should_escalate
from tests.conftest import GUILD_ID

OTHER_GUILD_ID = 999


def make_service(db: Database, store: GuildSettingsStore) -> ModerationService:
    return ModerationService(db, store)


@pytest.mark.parametrize(
    ("active", "threshold", "expected"),
    [
        (0, 3, False),
        (1, 3, False),
        (2, 3, False),
        (3, 3, True),
        (6, 3, True),
        (9, 3, True),
        (4, 3, False),
        (5, 0, False),
        (5, -1, False),
    ],
)
def test_escalation_matrix(active: int, threshold: int, expected: bool) -> None:
    assert should_escalate(active_warnings=active, threshold=threshold) is expected


async def test_case_numbers_start_at_one_and_increment(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    first = await service.record_action(GUILD_ID, action="kick", target_id=10, moderator_id=20)
    second = await service.record_action(GUILD_ID, action="ban", target_id=11, moderator_id=20)

    assert first.case_number == 1
    assert second.case_number == 2
    assert first.action_id != second.action_id


async def test_case_numbers_are_per_guild(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    here = await service.record_action(GUILD_ID, action="kick", target_id=10, moderator_id=20)
    there = await service.record_action(OTHER_GUILD_ID, action="kick", target_id=10, moderator_id=20)

    assert here.case_number == 1
    assert there.case_number == 1


async def test_record_action_keeps_details(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    record = await service.record_action(
        GUILD_ID,
        action="timeout",
        target_id=10,
        moderator_id=20,
        reason="刷屏",
        duration_seconds=600,
    )

    row = await db.fetchone("SELECT * FROM moderation_actions WHERE id = ?", (record.action_id,))
    assert row is not None
    assert row["action"] == "timeout"
    assert row["target_id"] == 10
    assert row["moderator_id"] == 20
    assert row["reason"] == "刷屏"
    assert row["duration_seconds"] == 600
    assert row["automated"] == 0
    assert row["revoked_at"] is None


async def test_warnings_accumulate(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    counts = [
        (await service.add_warning(GUILD_ID, target_id=10, moderator_id=20, reason=f"第 {n} 次"))[1]
        for n in range(1, 4)
    ]

    assert counts == [1, 2, 3]
    assert await service.active_warning_count(GUILD_ID, 10) == 3


async def test_warnings_are_per_target_and_guild(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    await service.add_warning(GUILD_ID, target_id=10, moderator_id=20)
    await service.add_warning(GUILD_ID, target_id=11, moderator_id=20)
    await service.add_warning(OTHER_GUILD_ID, target_id=10, moderator_id=20)

    assert await service.active_warning_count(GUILD_ID, 10) == 1
    assert await service.active_warning_count(GUILD_ID, 11) == 1
    assert await service.active_warning_count(OTHER_GUILD_ID, 10) == 1


async def test_warning_links_to_its_case(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    record, _ = await service.add_warning(GUILD_ID, target_id=10, moderator_id=20, reason="测试")

    row = await db.fetchone("SELECT * FROM warnings WHERE action_id = ?", (record.action_id,))
    assert row is not None
    assert row["guild_id"] == GUILD_ID
    assert row["target_id"] == 10
    assert row["reason"] == "测试"
    assert row["active"] == 1

    action = await db.fetchone("SELECT action FROM moderation_actions WHERE id = ?", (record.action_id,))
    assert action is not None
    assert action["action"] == "warn"


async def test_automated_actions_are_marked(db: Database, settings_store: GuildSettingsStore) -> None:
    service = make_service(db, settings_store)

    record = await service.record_action(
        GUILD_ID,
        action="auto_timeout",
        target_id=10,
        moderator_id=0,
        reason="累计警告",
        duration_seconds=3600,
        automated=True,
    )

    row = await db.fetchone("SELECT automated FROM moderation_actions WHERE id = ?", (record.action_id,))
    assert row is not None
    assert row["automated"] == 1
