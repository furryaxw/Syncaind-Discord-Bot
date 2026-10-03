"""数据层：迁移、事务、设置与模块状态读写。"""

from __future__ import annotations

from pathlib import Path

import pytest

from bot.core.database import Database
from bot.core.store import GuildSettingsStore, ModuleStateStore
from tests.conftest import GUILD_ID

EXPECTED_TABLES = {
    "guild_settings",
    "module_states",
    "moderation_actions",
    "warnings",
    "schema_version",
}


async def test_migration_creates_every_table(db: Database) -> None:
    rows = await db.fetchall("SELECT name FROM sqlite_master WHERE type = 'table'")

    assert EXPECTED_TABLES <= {row["name"] for row in rows}


async def test_migration_is_idempotent(settings) -> None:
    database = Database(settings.database_path)
    await database.connect()
    try:
        first = await database.migrate()
        second = await database.migrate()
    finally:
        await database.close()

    assert first == (1,)
    assert second == ()


async def test_operations_before_connect_are_rejected(tmp_path: Path) -> None:
    database = Database(tmp_path / "unconnected.db")

    with pytest.raises(RuntimeError):
        await database.execute("SELECT 1")


async def test_transaction_rolls_back_on_error(db: Database) -> None:
    with pytest.raises(RuntimeError):
        async with db.transaction() as connection:
            await connection.execute(
                "INSERT INTO module_states (guild_id, module_id, enabled, updated_at) VALUES (?, ?, ?, ?)",
                (GUILD_ID, "ghost", 1, "now"),
            )
            raise RuntimeError("boom")

    row = await db.fetchone("SELECT COUNT(*) AS total FROM module_states")
    assert row is not None
    assert row["total"] == 0


async def test_transaction_commits_on_success(db: Database) -> None:
    async with db.transaction() as connection:
        await connection.execute(
            "INSERT INTO module_states (guild_id, module_id, enabled, updated_at) VALUES (?, ?, ?, ?)",
            (GUILD_ID, "kept", 1, "now"),
        )

    row = await db.fetchone("SELECT enabled FROM module_states WHERE module_id = ?", ("kept",))
    assert row is not None
    assert row["enabled"] == 1


async def test_settings_defaults_when_no_row(settings_store: GuildSettingsStore) -> None:
    settings = await settings_store.get(GUILD_ID)

    assert settings.warn_threshold == 3
    assert settings.warn_action == "timeout"
    assert settings.warn_timeout_minutes == 60
    assert settings.mod_log_channel_id is None


async def test_settings_default_mod_log_channel_comes_from_env(db: Database) -> None:
    store = GuildSettingsStore(db, default_mod_log_channel_id=999)

    assert (await store.get(GUILD_ID)).mod_log_channel_id == 999


async def test_settings_update_round_trip(settings_store: GuildSettingsStore) -> None:
    await settings_store.update(GUILD_ID, warn_threshold=5, mod_log_channel_id=42)

    stored = await settings_store.get(GUILD_ID)

    assert stored.warn_threshold == 5
    assert stored.mod_log_channel_id == 42
    assert stored.warn_action == "timeout"
    assert stored.updated_at != ""


async def test_settings_update_rejects_unknown_field(settings_store: GuildSettingsStore) -> None:
    with pytest.raises(ValueError):
        await settings_store.update(GUILD_ID, nope=1)


async def test_settings_update_rejects_invalid_warn_action(settings_store: GuildSettingsStore) -> None:
    with pytest.raises(ValueError):
        await settings_store.update(GUILD_ID, warn_action="explode")


async def test_module_state_overrides_round_trip(module_state_store: ModuleStateStore) -> None:
    assert await module_state_store.overrides(GUILD_ID) == {}

    await module_state_store.set_enabled(GUILD_ID, "tools", False)
    await module_state_store.set_enabled(GUILD_ID, "moderation", True)

    assert await module_state_store.overrides(GUILD_ID) == {"tools": False, "moderation": True}

    await module_state_store.set_enabled(GUILD_ID, "tools", True)

    assert (await module_state_store.overrides(GUILD_ID))["tools"] is True
