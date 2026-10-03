"""测试夹具与共享 helper。"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from bot.core.config import Settings
from bot.core.database import Database
from bot.core.i18n import I18n, discover_module_locales
from bot.core.store import GuildSettingsStore, ModuleStateStore

GUILD_ID = 100
OWNER_ID = 200
LOCALES_DIR = REPO_ROOT / "bot" / "locales"
REAL_MODULES_DIR = REPO_ROOT / "bot" / "modules"
FIXTURE_MODULES_PACKAGE = "tests.fixture_modules"


@pytest.fixture
def settings(tmp_path: Path) -> Settings:
    return Settings(
        _env_file=None,
        discord_token="test-token",
        guild_id=GUILD_ID,
        owner_id=OWNER_ID,
        database_path=tmp_path / "bot.db",
        log_dir=tmp_path / "logs",
    )


@pytest.fixture
def i18n() -> I18n:
    """合并后的文案目录：核心 + 每个模块自带的 locales/。"""
    return I18n(LOCALES_DIR, "en-US", extra_sources=discover_module_locales(REAL_MODULES_DIR))


@pytest.fixture
async def db(settings: Settings) -> Database:
    database = Database(settings.database_path)
    await database.connect()
    await database.migrate()
    yield database
    await database.close()


@pytest.fixture
def settings_store(db: Database) -> GuildSettingsStore:
    return GuildSettingsStore(db)


@pytest.fixture
def module_state_store(db: Database) -> ModuleStateStore:
    return ModuleStateStore(db)


def fixture_modules_dir() -> Path:
    return Path(__file__).resolve().parent / "fixture_modules"
