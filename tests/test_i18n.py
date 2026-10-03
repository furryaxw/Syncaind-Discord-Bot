"""i18n：按语言取文案、缺键回退、占位符容错。"""

from __future__ import annotations

import json

import discord
import pytest

from bot.core.i18n import I18n, discover_module_locales


def test_headers_are_localized(i18n: I18n) -> None:
    assert i18n.t("zh-CN", "common.confirm") == "确认执行"
    assert i18n.t("en-US", "common.confirm") == "Confirm"


def test_chinese_locales_map_to_zh_cn(i18n: I18n) -> None:
    assert i18n.resolve("zh-CN") == "zh-CN"
    assert i18n.resolve("zh-TW") == "zh-CN"
    assert i18n.resolve(discord.Locale.chinese) == "zh-CN"


def test_everything_else_falls_back_to_default(i18n: I18n) -> None:
    assert i18n.resolve("fr") == "en-US"
    assert i18n.resolve(discord.Locale.french) == "en-US"
    assert i18n.resolve(None) == "en-US"
    assert i18n.resolve(123) == "en-US"


def test_missing_key_returns_the_key_instead_of_raising(i18n: I18n) -> None:
    assert i18n.t("zh-CN", "definitely.not.a.key") == "definitely.not.a.key"


def test_missing_key_in_one_locale_falls_back_to_default(tmp_path) -> None:
    (tmp_path / "en-US.json").write_text(json.dumps({"only.here": "english"}), encoding="utf-8")
    (tmp_path / "zh-CN.json").write_text(json.dumps({}), encoding="utf-8")
    i18n = I18n(tmp_path, "en-US")

    assert i18n.t("zh-CN", "only.here") == "english"


def test_placeholders_are_filled(i18n: I18n) -> None:
    assert "3" in i18n.t("zh-CN", "framework.module.list_description", count=3)


def test_missing_placeholder_does_not_raise(i18n: I18n) -> None:
    # 少传占位符时把原文吐出来，绝不能因为文案问题让命令崩掉。
    text = i18n.t("zh-CN", "framework.module.list_description")

    assert "{" in text


def test_default_locale_file_is_required(tmp_path) -> None:
    with pytest.raises(RuntimeError):
        I18n(tmp_path, "en-US")


def test_real_catalogs_are_loadable(i18n: I18n) -> None:
    assert set(i18n.available_locales) == {"en-US", "zh-CN"}


# ---------------------------------------------------------------- 多来源合并


def write_catalog(directory, locale: str, payload: dict[str, str]) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    (directory / f"{locale}.json").write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def test_sources_are_merged(tmp_path) -> None:
    core = tmp_path / "core"
    module = tmp_path / "mod"
    write_catalog(core, "en-US", {"a.b": "core"})
    write_catalog(module, "en-US", {"c.d": "module"})

    i18n = I18n(core, "en-US", extra_sources=[("mod", module)])

    assert i18n.t("en-US", "a.b") == "core"
    assert i18n.t("en-US", "c.d") == "module"
    assert i18n.duplicate_keys() == {}


def test_conflicting_key_keeps_the_first_source(tmp_path) -> None:
    """核心先读先得；模块不许悄悄盖掉框架文案，但冲突本身要能被发现。"""
    core = tmp_path / "core"
    module = tmp_path / "mod"
    write_catalog(core, "en-US", {"a.b": "core"})
    write_catalog(module, "en-US", {"a.b": "module"})

    i18n = I18n(core, "en-US", extra_sources=[("mod", module)])

    assert i18n.t("en-US", "a.b") == "core"
    assert i18n.duplicate_keys() == {"a.b": ("core", "mod")}


def test_missing_extra_source_is_ignored(tmp_path) -> None:
    core = tmp_path / "core"
    write_catalog(core, "en-US", {"a.b": "core"})

    i18n = I18n(core, "en-US", extra_sources=[("ghost", tmp_path / "nope")])

    assert i18n.t("en-US", "a.b") == "core"


def test_broken_catalog_file_is_skipped(tmp_path) -> None:
    """一个模块的文案文件写坏了，不该把核心文案一起拖下水。"""
    core = tmp_path / "core"
    module = tmp_path / "mod"
    write_catalog(core, "en-US", {"a.b": "core"})
    module.mkdir()
    (module / "en-US.json").write_text("{ not json", encoding="utf-8")

    i18n = I18n(core, "en-US", extra_sources=[("mod", module)])

    assert i18n.t("en-US", "a.b") == "core"


def test_reload_if_possible_keeps_the_previous_catalogs(tmp_path) -> None:
    core = tmp_path / "core"
    write_catalog(core, "en-US", {"a.b": "before"})
    i18n = I18n(core, "en-US")

    (core / "en-US.json").write_text("{ broken", encoding="utf-8")

    assert i18n.reload_if_possible() is False
    assert i18n.t("en-US", "a.b") == "before"


def test_reload_picks_up_new_text(tmp_path) -> None:
    core = tmp_path / "core"
    write_catalog(core, "en-US", {"a.b": "before"})
    i18n = I18n(core, "en-US")

    write_catalog(core, "en-US", {"a.b": "after"})

    assert i18n.reload_if_possible() is True
    assert i18n.t("en-US", "a.b") == "after"


def test_discover_module_locales_requires_a_real_package(tmp_path) -> None:
    good = tmp_path / "good"
    good.mkdir()
    (good / "__init__.py").write_text("", encoding="utf-8")
    write_catalog(good / "locales", "en-US", {"x.y": "ok"})

    stray = tmp_path / "stray"
    write_catalog(stray / "locales", "en-US", {"bad.key": "nope"})

    found = discover_module_locales(tmp_path)

    assert found == (("good", good / "locales"),)
