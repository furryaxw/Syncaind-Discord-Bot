"""i18n：多来源合并的文案目录，按客户端 locale 选语言，缺键回退默认语言。

文案可以来自多个目录：

* 核心目录 ``bot/locales/``：框架与通用词汇（``common.*`` / ``errors.*`` / ``framework.*`` / ``permissions.*``）；
* 每个模块自带的 ``bot/modules/<name>/locales/``：该模块自己的文案，随模块一起增删。

合并规则是**先到先得**：核心目录先读，模块后读；同一个键被两个来源定义时，采用先读到的那个，
并记一条 warning。这样「一个模块的笔误」不会悄悄覆盖框架文案。
跨文件重复键在 ``tests/test_locales.py`` 里是硬失败，所以正常情况下不会漂到这里。
"""

from __future__ import annotations

import json
import logging
from collections.abc import Iterable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CHINESE_TAG = "zh-CN"
CORE_LABEL = "core"


@dataclass(frozen=True)
class CatalogSource:
    """一份文案目录的来源。``label`` 只用于冲突提示与诊断。"""

    label: str
    directory: Path


def discover_module_locales(modules_dir: Path | str) -> tuple[tuple[str, Path], ...]:
    """找出每个模块自带的 ``locales/`` 目录，返回 ``[(module_id, path), ...]``。

    只认同时存在 ``__init__.py`` 与 ``locales/`` 的目录，避免一个游离目录往文案里注入内容。
    """
    root = Path(modules_dir)
    if not root.is_dir():
        return ()
    found: list[tuple[str, Path]] = []
    for path in sorted(root.iterdir()):
        locales = path / "locales"
        if path.is_dir() and (path / "__init__.py").is_file() and locales.is_dir():
            found.append((path.name, locales))
    return tuple(found)


class I18n:
    """加载并合并若干文案目录，提供 ``t(locale, key, **kwargs)``。"""

    def __init__(
        self,
        locales_dir: Path | str,
        default_locale: str = "en-US",
        logger: logging.Logger | None = None,
        extra_sources: Iterable[tuple[str, Path]] = (),
    ) -> None:
        self._logger = logger or logging.getLogger("bot.i18n")
        self._default_locale = default_locale
        self._sources: tuple[CatalogSource, ...] = (
            CatalogSource(CORE_LABEL, Path(locales_dir)),
            *(CatalogSource(label, Path(directory)) for label, directory in extra_sources),
        )
        self._catalogs: dict[str, dict[str, str]] = {}
        self._duplicates: dict[tuple[str, str], list[str]] = {}
        self.reload()

    # ---------------------------------------------------------------- 加载

    @property
    def sources(self) -> tuple[CatalogSource, ...]:
        return self._sources

    def reload(self) -> None:
        """重新读取全部来源。读不出默认语言时抛错，并保留上一次的目录不变。"""
        catalogs, duplicates, failures = self._read_sources()
        if self._default_locale not in catalogs:
            raise RuntimeError(
                f"默认语言 {self._default_locale} 没有可用的文案文件"
                f"（读取来源：{', '.join(str(source.directory) for source in self._sources)}）"
            )
        self._catalogs = catalogs
        self._duplicates = duplicates
        for (locale, key), labels in duplicates.items():
            self._logger.warning(
                "文案键冲突：%s（语言 %s）同时定义在 %s，采用先读到的 %s",
                key,
                locale,
                " / ".join(labels),
                labels[0],
            )
        if failures:
            self._logger.warning("有 %d 个文案文件读取失败，相关文案会回退到默认语言", len(failures))

    def reload_if_possible(self) -> bool:
        """尽量重载：失败时保留旧目录并返回 ``False``。

        供模块热重载调用——一个模块的文案文件写错了，不该把已经在跑的机器人搞坏。
        """
        try:
            self.reload()
            return True
        except Exception:
            self._logger.exception("重新加载文案失败，继续使用上一次的目录")
            return False

    def _read_sources(
        self,
    ) -> tuple[dict[str, dict[str, str]], dict[tuple[str, str], list[str]], list[str]]:
        catalogs: dict[str, dict[str, str]] = {}
        origins: dict[tuple[str, str], str] = {}
        duplicates: dict[tuple[str, str], list[str]] = {}
        failures: list[str] = []

        for source in self._sources:
            if not source.directory.is_dir():
                self._logger.debug("文案目录不存在，跳过：%s", source.directory)
                continue
            for path in sorted(source.directory.glob("*.json")):
                data = self._read_file(path, failures)
                if data is None:
                    continue
                bucket = catalogs.setdefault(path.stem, {})
                for key, value in data.items():
                    if not isinstance(value, str):
                        self._logger.warning("忽略非字符串文案：%s（%s）", key, path.name)
                        continue
                    if (path.stem, key) in origins:
                        duplicates.setdefault((path.stem, key), [origins[(path.stem, key)]]).append(source.label)
                        continue
                    origins[(path.stem, key)] = source.label
                    bucket[key] = value
        return catalogs, duplicates, failures

    def _read_file(self, path: Path, failures: list[str]) -> dict[str, Any] | None:
        try:
            with path.open(encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, json.JSONDecodeError):
            failures.append(str(path))
            self._logger.exception("文案文件无法读取，已跳过：%s", path)
            return None
        if not isinstance(data, dict):
            failures.append(str(path))
            self._logger.error("文案文件的顶层必须是对象，已跳过：%s", path)
            return None
        return data

    # ---------------------------------------------------------------- 诊断

    @property
    def default_locale(self) -> str:
        return self._default_locale

    @property
    def available_locales(self) -> tuple[str, ...]:
        return tuple(sorted(self._catalogs))

    def catalog(self, locale: str) -> dict[str, str]:
        """某个语言合并后的文案（副本，供诊断与测试）。"""
        return dict(self._catalogs.get(self.resolve(locale), {}))

    def duplicate_keys(self) -> dict[str, tuple[str, ...]]:
        """被多个来源定义过的键 → 来源列表。正常情况下应该是空的。"""
        merged: dict[str, set[str]] = {}
        for (_locale, key), labels in self._duplicates.items():
            merged.setdefault(key, set()).update(labels)
        return {key: tuple(sorted(labels)) for key, labels in merged.items()}

    # ---------------------------------------------------------------- 取用

    def resolve(self, locale: Any) -> str:
        """把 discord 的 ``Locale`` 或字符串映射到本项目的语言标签。"""
        tag = getattr(locale, "value", locale)
        if not isinstance(tag, str):
            return self._default_locale
        normalized = tag.strip()
        if normalized.lower().startswith("zh") and CHINESE_TAG in self._catalogs:
            return CHINESE_TAG
        if normalized in self._catalogs:
            return normalized
        return self._default_locale

    def lookup(self, locale: Any, key: str) -> str | None:
        """只查目录，不把键名当兜底。查不到返回 ``None``。

        命令元数据的翻译需要这个：discord.py 收到 ``None`` 会保留英文原文，
        而 ``t()`` 会把键名吐出来——那个不该出现在用户界面上。
        """
        return self._lookup(self.resolve(locale), key)

    def t(self, locale: Any, key: str, **kwargs: Any) -> str:
        """取文案。``locale`` 可以是标签、``discord.Locale`` 或 ``None``。"""
        tag = self.resolve(locale)
        text = self._lookup(tag, key)
        if text is None:
            self._logger.warning("缺少翻译键 %s（语言 %s）", key, tag)
            return key
        if not kwargs:
            return text
        try:
            return text.format(**kwargs)
        except (KeyError, IndexError, ValueError) as exc:
            self._logger.warning("翻译键 %s 的占位符无法填充：%s", key, exc)
            return text

    def _lookup(self, tag: str, key: str) -> str | None:
        catalog = self._catalogs.get(tag, {})
        if key in catalog:
            return catalog[key]
        return self._catalogs.get(self._default_locale, {}).get(key)
