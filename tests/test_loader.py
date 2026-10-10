"""模块加载器：失败隔离、依赖检查、运行时启停、热重载。"""

from __future__ import annotations

import logging
import sys
from pathlib import Path

import pytest

from bot.core.loader import ModuleLoader
from bot.core.module import ModuleStatus
from bot.core.store import ModuleStateStore
from tests.conftest import FIXTURE_MODULES_PACKAGE, GUILD_ID, fixture_modules_dir
from tests.fakes import FakeBot

ALL_FIXTURES = {
    "aaa_consumer",
    "bad_meta",
    "broken",
    "good",
    "missing_dep",
    "no_meta",
    "off_by_default",
    "setup_fails",
    "with_dep",
    "zzz_provider",
}


def make_loader(
    bot: FakeBot,
    state_store: ModuleStateStore,
    *,
    modules_dir: Path | None = None,
    package: str = FIXTURE_MODULES_PACKAGE,
) -> ModuleLoader:
    return ModuleLoader(
        bot,
        guild_id=GUILD_ID,
        modules_dir=modules_dir or fixture_modules_dir(),
        state_store=state_store,
        package=package,
    )


async def test_discover_lists_every_fixture(module_state_store: ModuleStateStore) -> None:
    loader = make_loader(FakeBot(), module_state_store)

    assert set(loader.discover()) == ALL_FIXTURES


async def test_good_module_loads(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)

    info = await loader.load("good")

    assert info.status is ModuleStatus.LOADED
    assert info.error is None
    assert "GoodCog" in bot.cogs
    assert loader.loaded_ids == ("good",)


async def test_a_broken_module_does_not_stop_the_others(module_state_store: ModuleStateStore) -> None:
    """这是整个模块化设计的第一条硬要求。"""
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)

    await loader.load_all()

    statuses = {info.id: info.status for info in loader.infos()}
    assert statuses["good"] is ModuleStatus.LOADED
    assert statuses["with_dep"] is ModuleStatus.LOADED
    assert statuses["aaa_consumer"] is ModuleStatus.LOADED
    assert statuses["zzz_provider"] is ModuleStatus.LOADED
    assert statuses["broken"] is ModuleStatus.FAILED
    assert statuses["no_meta"] is ModuleStatus.FAILED
    assert statuses["bad_meta"] is ModuleStatus.FAILED
    assert statuses["setup_fails"] is ModuleStatus.FAILED
    assert "GoodCog" in bot.cogs


async def test_failure_records_where_it_broke(module_state_store: ModuleStateStore) -> None:
    loader = make_loader(FakeBot(), module_state_store)

    broken = await loader.load("broken")
    bad_meta = await loader.load("bad_meta")
    setup_failed = await loader.load("setup_fails")

    assert broken.error is not None and "导入阶段就炸了" in broken.error
    assert bad_meta.error is not None and "MODULE_META" in bad_meta.error
    assert setup_failed.error is not None and setup_failed.error.startswith("setup:")


async def test_setup_failure_leaves_no_cog_behind(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)

    await loader.load("setup_fails")

    assert bot.cogs == {}


async def test_missing_dependency_degrades_instead_of_crashing(
    module_state_store: ModuleStateStore,
) -> None:
    loader = make_loader(FakeBot(), module_state_store)

    info = await loader.load("missing_dep")

    assert info.status is ModuleStatus.DEPENDENCY_MISSING
    assert info.error is not None and "does_not_exist" in info.error


async def test_dependency_loading_order(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)

    await loader.load("with_dep")
    assert loader.info("with_dep").status is ModuleStatus.DEPENDENCY_MISSING  # good 还没加载

    await loader.load("good")
    info = await loader.load("with_dep")

    assert info.status is ModuleStatus.LOADED
    assert {"GoodCog", "WithDepCog"} <= set(bot.cogs)


async def test_direct_load_still_warns_about_a_missing_dependency(
    module_state_store: ModuleStateStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """直接 load（比如 /module enable）时，缺依赖必须立刻可见——那时没有「下一轮」。"""
    loader = make_loader(FakeBot(), module_state_store)

    with caplog.at_level(logging.WARNING, logger="bot.loader"):
        await loader.load("missing_dep")

    assert "missing_dep" in caplog.text


async def test_load_all_only_warns_about_dependencies_it_could_not_resolve(
    module_state_store: ModuleStateStore,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """启动日志里不该出现「依赖未加载」——那只是本轮还没轮到，不是故障。

    实测过一次：每次启动都打一行「模块 cases 未加载：依赖未加载：moderation」，
    看着像故障，其实第二轮就加载成功了。真的缺依赖（``missing_dep``）仍然要告警。
    """
    loader = make_loader(FakeBot(), module_state_store)

    with caplog.at_level(logging.WARNING, logger="bot.loader"):
        await loader.load_all()

    assert "aaa_consumer" not in caplog.text, "它只是排在依赖前面，不该告警"
    assert "with_dep" not in caplog.text
    assert "missing_dep" in caplog.text, "真的缺依赖必须告警"
    assert loader.info("aaa_consumer").status is ModuleStatus.LOADED


async def test_load_all_resolves_dependencies_regardless_of_name_order(
    module_state_store: ModuleStateStore,
) -> None:
    """回归测试：依赖方 id 按字母序排在前面时，也必须先加载被依赖方。

    ``aaa_consumer`` 依赖 ``zzz_provider`` 而排在它前面——按字母序加载会把它判成依赖未满足。
    """
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)

    await loader.load_all()

    statuses = {info.id: info.status for info in loader.infos()}
    assert statuses["zzz_provider"] is ModuleStatus.LOADED
    assert statuses["aaa_consumer"] is ModuleStatus.LOADED
    assert {"AaaCog", "ZzzCog"} <= set(bot.cogs)


async def test_off_by_default_is_not_loaded(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)

    info = await loader.load("off_by_default")

    assert info.status is ModuleStatus.DISABLED
    assert bot.cogs == {}


async def test_enable_persists_and_takes_effect(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)
    await loader.load_all()
    assert "OffCog" not in bot.cogs

    info = await loader.set_enabled("off_by_default", True)

    assert info.status is ModuleStatus.LOADED
    assert "OffCog" in bot.cogs
    assert await module_state_store.overrides(GUILD_ID) == {"off_by_default": True}

    # 新加载器读同一份状态，应该也是启用。
    reloaded = make_loader(FakeBot(), module_state_store)
    await reloaded.load_all()
    assert reloaded.info("off_by_default").status is ModuleStatus.LOADED


async def test_disable_unloads_the_cog_and_persists(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)
    await loader.load_all()
    assert "GoodCog" in bot.cogs

    info = await loader.set_enabled("good", False)

    assert info.status is ModuleStatus.DISABLED
    assert "GoodCog" not in bot.cogs
    assert await module_state_store.overrides(GUILD_ID) == {"good": False}

    fresh = make_loader(FakeBot(), module_state_store)
    await fresh.load_all()
    assert fresh.info("good").status is ModuleStatus.DISABLED


async def test_reload_picks_up_new_code(tmp_path: Path, monkeypatch, module_state_store: ModuleStateStore) -> None:
    """热重载的核心验收：改文件后不重启进程也能生效。"""
    package_dir = tmp_path / "hotpkg"
    module_dir = package_dir / "reloadable"
    module_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    write_versioned_module(module_dir, "v1")
    monkeypatch.syspath_prepend(str(tmp_path))

    bot = FakeBot()
    loader = make_loader(bot, module_state_store, modules_dir=package_dir, package="hotpkg")

    assert (await loader.load("reloadable")).status is ModuleStatus.LOADED
    assert bot.cogs["VersionedCog"].version == "v1"

    write_versioned_module(module_dir, "v2")
    info = await loader.reload("reloadable")

    assert info.status is ModuleStatus.LOADED
    assert bot.cogs["VersionedCog"].version == "v2"
    assert sys.modules["hotpkg.reloadable"].VERSION == "v2"


async def test_reload_rereads_a_data_file_a_submodule_reads(
    tmp_path: Path,
    monkeypatch,
    module_state_store: ModuleStateStore,
) -> None:
    """子模块在导入时读的数据文件，重载后也要重新读。

    `forms` 的表单定义就是这种数据文件（`definitions/*.json` 由 `schema.py` 在导入时读取），
    所以「改完 `/module reload` 生效」这条承诺依赖的正是这个行为：子模块必须被重新导入，
    而不是从 `sys.modules` 里拿到缓存的旧对象。
    """
    package_dir = tmp_path / "datapkg"
    module_dir = package_dir / "dataful"
    module_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (module_dir / "labels.json").write_text('{"label": "v1"}', encoding="utf-8")
    (module_dir / "schema.py").write_text(
        "import json\n"
        "from pathlib import Path\n"
        "LABEL = json.loads(Path(__file__).with_name('labels.json').read_text(encoding='utf-8'))['label']\n",
        encoding="utf-8",
    )
    (module_dir / "cog.py").write_text(
        "from discord.ext import commands\n"
        "\n"
        "\n"
        "class DataCog(commands.Cog):\n"
        "    def __init__(self, label: str) -> None:\n"
        "        self.label = label\n",
        encoding="utf-8",
    )
    (module_dir / "__init__.py").write_text(
        "from bot.core.module import ModuleMeta\n"
        "\n"
        "from .cog import DataCog\n"
        "from .schema import LABEL\n"
        "\n"
        "MODULE_META = ModuleMeta(id='dataful')\n"
        "\n"
        "\n"
        "async def setup(bot) -> None:\n"
        "    await bot.add_cog(DataCog(LABEL))\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))

    bot = FakeBot()
    loader = make_loader(bot, module_state_store, modules_dir=package_dir, package="datapkg")

    assert (await loader.load("dataful")).status is ModuleStatus.LOADED
    assert bot.cogs["DataCog"].label == "v1"

    (module_dir / "labels.json").write_text('{"label": "v2"}', encoding="utf-8")

    assert (await loader.reload("dataful")).status is ModuleStatus.LOADED
    assert bot.cogs["DataCog"].label == "v2"


async def test_reload_keeps_the_module_disabled(module_state_store: ModuleStateStore) -> None:
    bot = FakeBot()
    loader = make_loader(bot, module_state_store)
    await loader.set_enabled("good", False)

    info = await loader.reload("good")

    assert info.status is ModuleStatus.DISABLED
    assert bot.cogs == {}


async def test_unknown_module_is_reported_as_none(module_state_store: ModuleStateStore) -> None:
    loader = make_loader(FakeBot(), module_state_store)

    await loader.load_all()

    assert loader.info("nope") is None


async def test_infos_are_sorted_by_id(module_state_store: ModuleStateStore) -> None:
    loader = make_loader(FakeBot(), module_state_store)

    await loader.load_all()

    ids = [info.id for info in loader.infos()]
    assert ids == sorted(ids)


async def test_missing_modules_dir_is_not_fatal(tmp_path: Path, module_state_store: ModuleStateStore) -> None:
    loader = make_loader(FakeBot(), module_state_store, modules_dir=tmp_path / "nope", package=FIXTURE_MODULES_PACKAGE)

    assert loader.discover() == []


def write_versioned_module(module_dir: Path, version: str) -> None:
    """写一个「代码里带着版本号」的模块，用来证明重载后跑的是新代码。"""
    (module_dir / "__init__.py").write_text(
        f'''"""热重载测试模块：当前版本 {version}。"""

from __future__ import annotations

from typing import Any

from bot.core.module import ModuleMeta

VERSION = "{version}"

MODULE_META = ModuleMeta(id="reloadable")


class VersionedCog:
    def __init__(self, bot: Any) -> None:
        self.bot = bot
        self.version = VERSION


async def setup(bot: Any) -> None:
    await bot.add_cog(VersionedCog(bot))
''',
        encoding="utf-8",
    )
