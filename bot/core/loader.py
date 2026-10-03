"""模块加载器：目录自动扫描、失败隔离、运行时启停、热重载。"""

from __future__ import annotations

import importlib
import inspect
import logging
import shutil
import sys
import traceback
from collections.abc import Iterable
from pathlib import Path
from types import ModuleType
from typing import Any

from .module import ModuleInfo, ModuleMeta, ModuleStatus
from .store import ModuleStateStore

DEFAULT_PACKAGE = "bot.modules"


class ModuleMetaError(RuntimeError):
    """模块自述不合法（缺 MODULE_META、类型不对、id 与目录名不一致等）。"""


class ModuleLoader:
    """管理 ``<package>/<module_id>/`` 下的模块。

    设计要点：

    * 单个模块失败**只隔离它自己**，机器人照常启动；
    * 停用与失败是两种状态，``/module list`` 要能分辨；
    * 模块「加了哪些 Cog」由加载前后的 ``bot.cogs`` 差集推导，卸载时精确摘掉，
      模块不需要自己写卸载逻辑。
    """

    def __init__(
        self,
        bot: Any,
        *,
        guild_id: int,
        modules_dir: Path | str,
        state_store: ModuleStateStore,
        package: str = DEFAULT_PACKAGE,
        logger: logging.Logger | None = None,
    ) -> None:
        self._bot = bot
        self._guild_id = guild_id
        self._modules_dir = Path(modules_dir)
        self._state_store = state_store
        self._package = package
        self._logger = logger or logging.getLogger("bot.loader")
        self._infos: dict[str, ModuleInfo] = {}
        self._cogs: dict[str, list[str]] = {}
        self._overrides: dict[str, bool] = {}

    # ---------------------------------------------------------------- 发现

    def discover(self) -> list[str]:
        """列出候选模块 id（按目录名排序）。不导入任何代码。"""
        if not self._modules_dir.is_dir():
            self._logger.warning("模块目录不存在：%s", self._modules_dir)
            return []
        return sorted(
            path.name
            for path in self._modules_dir.iterdir()
            if path.is_dir() and (path / "__init__.py").is_file() and not path.name.startswith("_")
        )

    # ---------------------------------------------------------------- 加载

    async def load_all(self) -> None:
        """加载所有模块。任何单个模块的失败都不会向上抛。

        **依赖顺序不靠字母序。** 模块 id 的字母序和依赖方向没有关系
        （``cases`` 依赖 ``moderation``，而它恰好排在前面），所以这里分多轮加载：
        某一轮里依赖尚未满足的模块留到下一轮；一轮下来毫无进展就说明剩下的要么真的缺依赖、
        要么依赖成环——它们的状态已经记成「依赖未满足」，日志里能看到原因。
        """
        self._overrides = await self._state_store.overrides(self._guild_id)
        pending = self.discover()
        while pending:
            deferred: list[str] = []
            for module_id in pending:
                # 多轮加载里「这一轮还轮不到」是正常现象，先不要告警——否则每次启动
                # 都会出现一行「模块 X 未加载：依赖未加载：Y」，看着像故障其实不是。
                info = await self.load(module_id, quiet_dependency=True)
                if info.status is ModuleStatus.DEPENDENCY_MISSING:
                    deferred.append(module_id)
            if len(deferred) == len(pending):
                # 一轮下来毫无进展：这些才是真的缺依赖或依赖成环，现在值得告警。
                for module_id in deferred:
                    self._logger.warning("模块 %s 未加载：%s", module_id, self._infos[module_id].error)
                break
            pending = deferred

    async def load(self, module_id: str, *, quiet_dependency: bool = False) -> ModuleInfo:
        """加载单个模块。失败被记录进状态，不抛出。"""
        self._cogs.setdefault(module_id, [])
        try:
            package = self._import_package(module_id)
            meta = _read_meta(package, module_id)
        except Exception as exc:
            return self._mark_failed(module_id, exc, "_read_meta")

        info = ModuleInfo(meta=meta, status=ModuleStatus.DISABLED)

        if not self._is_enabled(meta):
            self._infos[module_id] = info
            self._logger.info("模块 %s 已停用，跳过加载", module_id)
            return info

        missing = [dep for dep in meta.depends_on if not self._is_loaded(dep)]
        if missing:
            info.status = ModuleStatus.DEPENDENCY_MISSING
            info.error = f"依赖未加载：{', '.join(missing)}"
            self._infos[module_id] = info
            if quiet_dependency:
                self._logger.debug("模块 %s 本轮跳过：%s", module_id, info.error)
            else:
                self._logger.warning("模块 %s 未加载：%s", module_id, info.error)
            return info

        setup = getattr(package, "setup", None)
        if not callable(setup):
            return self._mark_failed(module_id, ModuleMetaError("模块缺少 setup(bot)"), "_read_meta", meta)

        before = set(self._bot.cogs)
        try:
            result = setup(self._bot)
            if inspect.isawaitable(result):
                await result
        except Exception as exc:
            await self._remove_cogs(set(self._bot.cogs) - before)
            return self._mark_failed(module_id, exc, "setup", meta)

        self._cogs[module_id] = sorted(set(self._bot.cogs) - before)
        info.status = ModuleStatus.LOADED
        info.error = None
        self._infos[module_id] = info
        self._logger.info("模块 %s 已加载（Cog：%s）", module_id, ", ".join(self._cogs[module_id]) or "无")
        return info

    async def unload(self, module_id: str) -> None:
        """摘掉模块注册的 Cog。模块本身留在 sys.modules 里。"""
        names = self._cogs.pop(module_id, [])
        await self._remove_cogs(set(names))

    async def reload(self, module_id: str) -> ModuleInfo:
        """卸载 → 清理字节码与 sys.modules → 重新导入并加载。"""
        await self.unload(module_id)
        self._purge_bytecode(module_id)
        self._purge_modules(module_id)
        self._infos.pop(module_id, None)
        return await self.load(module_id)

    async def set_enabled(self, module_id: str, enabled: bool) -> ModuleInfo:
        """持久化开关并立即生效。"""
        await self._state_store.set_enabled(self._guild_id, module_id, enabled)
        self._overrides[module_id] = enabled
        if enabled:
            info = await self.load(module_id)
        else:
            await self.unload(module_id)
            info = self._infos.get(module_id)
            if info is None:
                # 之前从未加载过（例如启动时就是停用状态），补一次元数据读取。
                info = await self.load(module_id)
            info.status = ModuleStatus.DISABLED
            info.error = None
            self._infos[module_id] = info
        return info

    # ---------------------------------------------------------------- 查询

    def infos(self) -> list[ModuleInfo]:
        return [self._infos[module_id] for module_id in sorted(self._infos)]

    def info(self, module_id: str) -> ModuleInfo | None:
        return self._infos.get(module_id)

    def module_of_cog(self, cog_name: str) -> str | None:
        """反查某个 Cog 是哪个模块注册的。``/help`` 按模块分组要用。"""
        for module_id, names in self._cogs.items():
            if cog_name in names:
                return module_id
        return None

    @property
    def loaded_ids(self) -> tuple[str, ...]:
        return tuple(
            module_id for module_id in sorted(self._infos) if self._infos[module_id].status is ModuleStatus.LOADED
        )

    # ---------------------------------------------------------------- 内部

    def _is_enabled(self, meta: ModuleMeta) -> bool:
        return self._overrides.get(meta.id, meta.default_enabled)

    def _is_loaded(self, module_id: str) -> bool:
        info = self._infos.get(module_id)
        return info is not None and info.status is ModuleStatus.LOADED

    def _mark_failed(
        self,
        module_id: str,
        exc: BaseException,
        stage: str,
        meta: ModuleMeta | None = None,
    ) -> ModuleInfo:
        self._logger.error(
            "模块 %s 在 %s 阶段加载失败：%s\n%s",
            module_id,
            stage,
            exc,
            "".join(traceback.format_exception(type(exc), exc, exc.__traceback__)),
        )
        fallback = meta or ModuleMeta(id=module_id)
        info = ModuleInfo(
            meta=fallback,
            status=ModuleStatus.FAILED,
            error=f"{stage}: {type(exc).__name__}: {exc}",
        )
        self._infos[module_id] = info
        return info

    def _import_package(self, module_id: str) -> ModuleType:
        importlib.invalidate_caches()
        return importlib.import_module(f"{self._package}.{module_id}")

    def _purge_modules(self, module_id: str) -> None:
        """把模块包及其子模块从 sys.modules 里清掉，否则热重载拿不到新代码。"""
        prefix = f"{self._package}.{module_id}"
        for name in [name for name in sys.modules if name == prefix or name.startswith(prefix + ".")]:
            del sys.modules[name]

    def _purge_bytecode(self, module_id: str) -> None:
        """删掉这个模块的 ``__pycache__``。

        CPython 判断 .pyc 是否可复用只看「源文件 mtime + 文件大小」。同一秒内改出一个
        大小相同的文件（把 ``v1`` 改成 ``v2`` 就是），旧字节码会被判定为仍然有效，
        于是出现「改了代码但热重载没生效」。开发时这种改动太常见，所以宁可每次重载都清掉。
        """
        module_dir = self._modules_dir / module_id
        if not module_dir.is_dir():
            return
        for cache_dir in module_dir.rglob("__pycache__"):
            shutil.rmtree(cache_dir, ignore_errors=True)

    async def _remove_cogs(self, names: Iterable[str]) -> None:
        for name in sorted(names):
            try:
                await self._bot.remove_cog(name)
            except Exception:
                self._logger.exception("移除 Cog %s 失败", name)


def _read_meta(package: ModuleType, module_id: str) -> ModuleMeta:
    meta = getattr(package, "MODULE_META", None)
    if not isinstance(meta, ModuleMeta):
        raise ModuleMetaError(f"模块 {module_id} 缺少合法的 MODULE_META（应为 ModuleMeta 实例）")
    if meta.id != module_id:
        raise ModuleMetaError(f"MODULE_META.id={meta.id!r} 与目录名 {module_id!r} 不一致")
    return meta
