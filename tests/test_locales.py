"""文案完整性：合并后的两份目录键集一致、代码里用到的键都存在、没有跨文件重复键。

这类测试的价值在于「防漂移」——加了新文案键却忘了另一门语言、键名打错一个字母、
两个模块用了同一个键，都会在这里被拦住，而不是等到线上某个用户看到 `moderation.done.title` 这样的原文。
"""

from __future__ import annotations

import importlib
import re
import string
from pathlib import Path
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands

from bot.core.framework import FrameworkCog
from bot.core.help import HelpCog
from bot.core.i18n import I18n, discover_module_locales
from bot.modules.tools.cog import BOT_PERMISSIONS, MEMBER_PERMISSIONS, ToolsCog

REPO_ROOT = Path(__file__).resolve().parents[1]
LOCALES_DIR = REPO_ROOT / "bot" / "locales"
MODULES_DIR = REPO_ROOT / "bot" / "modules"
SOURCE_DIR = REPO_ROOT / "bot"

KEY_PREFIXES = (
    "errors.",
    "common.",
    "actions.",
    "framework.",
    "permissions.",
    "moderation.",
    "cases.",
    "channels.",
    "roles.",
    "reaction_roles.",
    "tools.",
    "modules.",
    "commands.",
    "github.",
    "feed.",
    "keys.",
    "access.",
    "smas.",
    "forms.",
)
KEY_PATTERN = re.compile(r"""["']([A-Za-z_]+(?:\.[A-Za-z_]+)+)["']""")

# 代码里用 f-string 拼出来的键，扫描器看不到，这里显式列出。
ACTION_KEYS = (
    "kick",
    "ban",
    "unban",
    "timeout",
    "warn",
    "purge",
    "auto_timeout",
    "auto_kick",
    "auto_ban",
)


def all_cog_types() -> tuple[type, ...]:
    """所有真实 Cog 类。

    这份登记表决定了下面对「命令描述」「权限声明」「声明与检查是否一致」的检查覆盖到哪些模块。
    漏登记一个模块，那些检查就会静默跳过它——所以它有一个完整性测试盯着
    （``test_cog_registry_covers_every_module``），别只靠人记。
    """
    from bot.modules.access_keys.cog import AccessKeysCog
    from bot.modules.access_roles.cog import AccessRolesCog
    from bot.modules.cases.cog import CasesCog
    from bot.modules.channels.cog import ChannelsCog
    from bot.modules.forms.cog import FormsCog
    from bot.modules.github_bridge.cog import GitHubBridgeCog
    from bot.modules.github_feed.cog import GitHubFeedCog
    from bot.modules.moderation.cog import ModerationCog as RealModerationCog
    from bot.modules.reaction_roles.cog import ReactionRolesCog
    from bot.modules.roles.cog import RolesCog

    return (
        FrameworkCog,
        HelpCog,
        RealModerationCog,
        CasesCog,
        ChannelsCog,
        RolesCog,
        ReactionRolesCog,
        ToolsCog,
        GitHubBridgeCog,
        GitHubFeedCog,
        AccessKeysCog,
        AccessRolesCog,
        FormsCog,
    )


def test_cog_registry_covers_every_module() -> None:
    """磁盘上每个模块都必须有 Cog 登记在册，否则一致性检查会把它漏掉。"""
    module_ids = {path.name for path in MODULES_DIR.iterdir() if path.is_dir() and (path / "__init__.py").is_file()}
    covered = {
        parts[2]
        for cog_type in all_cog_types()
        if (parts := cog_type.__module__.split("."))[:2] == ["bot", "modules"] and len(parts) > 2
    }

    assert covered == module_ids


@pytest.fixture(scope="module")
def i18n() -> I18n:
    return I18n(LOCALES_DIR, "en-US", extra_sources=discover_module_locales(MODULES_DIR))


@pytest.fixture(scope="module")
def catalogs(i18n: I18n) -> dict[str, dict[str, str]]:
    return {locale: i18n.catalog(locale) for locale in i18n.available_locales}


def all_commands() -> list[app_commands.Command]:
    """所有真实命令对象，含分组里的子命令。"""
    found: list[app_commands.Command] = []
    for cog_type in all_cog_types():
        for value in vars(cog_type).values():
            if isinstance(value, app_commands.Group):
                found.extend(value.commands)
            elif isinstance(value, app_commands.Command):
                found.append(value)
    return found


def placeholders(text: str) -> set[str]:
    return {name for _literal, name, _spec, _conv in string.Formatter().parse(text) if name}


def scan_source_keys() -> set[str]:
    found: set[str] = set()
    for path in SOURCE_DIR.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for match in KEY_PATTERN.finditer(text):
            key = match.group(1)
            if key.startswith(KEY_PREFIXES):
                found.add(key)
    return found


# ---------------------------------------------------------------- 目录结构


def test_every_module_directory_ships_its_own_locales(i18n: I18n) -> None:
    """每个模块都必须自带 locales/ —— 这是自包含契约的一部分，不是可选项。"""
    module_ids = {path.name for path in MODULES_DIR.iterdir() if path.is_dir() and (path / "__init__.py").is_file()}
    labels = {source.label for source in i18n.sources} - {"core"}

    assert labels == module_ids
    assert i18n.sources[0].label == "core", "核心文案必须先读，先到先得才有意义"


def test_both_catalogs_have_the_same_keys(catalogs: dict[str, dict[str, str]]) -> None:
    assert set(catalogs["zh-CN"]) == set(catalogs["en-US"])


def test_no_duplicate_keys_across_sources(i18n: I18n) -> None:
    """同一个键不许同时出现在核心与模块、或两个模块里（先到先得会静默吃掉后一个）。"""
    assert i18n.duplicate_keys() == {}


def test_placeholders_match_across_locales(catalogs: dict[str, dict[str, str]]) -> None:
    mismatched = {
        key: (placeholders(catalogs["zh-CN"][key]), placeholders(catalogs["en-US"][key]))
        for key in catalogs["zh-CN"]
        if placeholders(catalogs["zh-CN"][key]) != placeholders(catalogs["en-US"][key])
    }

    assert mismatched == {}


def test_no_catalog_value_is_blank(catalogs: dict[str, dict[str, str]]) -> None:
    blank = [key for catalog in catalogs.values() for key, value in catalog.items() if not value.strip()]

    assert blank == []


def test_no_orphan_keys_outside_known_prefixes(catalogs: dict[str, dict[str, str]]) -> None:
    orphans = [key for key in catalogs["en-US"] if not key.startswith(KEY_PREFIXES)]

    assert orphans == []


# ---------------------------------------------------------------- 键与代码一致


def test_every_key_used_in_source_exists(catalogs: dict[str, dict[str, str]]) -> None:
    used = scan_source_keys()

    assert used, "扫描器没扫到任何键，说明它自己坏了"
    missing = sorted(used - set(catalogs["en-US"]))
    assert missing == []


def test_action_keys_exist(catalogs: dict[str, dict[str, str]]) -> None:
    missing = sorted(key for action in ACTION_KEYS if (key := f"actions.{action}") not in catalogs["en-US"])

    assert missing == []


def test_permission_keys_exist(catalogs: dict[str, dict[str, str]]) -> None:
    missing = sorted(
        key
        for name in set(MEMBER_PERMISSIONS) | set(BOT_PERMISSIONS)
        if (key := f"permissions.{name}") not in catalogs["en-US"]
    )

    assert missing == []


def test_commands_declare_permission_labels(catalogs: dict[str, dict[str, str]]) -> None:
    """/help 要展示「需要哪些权限」，所以 extras 里声明的每一项都得有对应文案。"""
    missing = sorted(
        f"{command.qualified_name} → permissions.{flag}"
        for command in all_commands()
        for flag in (command.extras or {}).get("permissions") or ()
        if f"permissions.{flag}" not in catalogs["en-US"]
    )

    assert missing == []


def test_module_meta_keys_exist(catalogs: dict[str, dict[str, str]]) -> None:
    """每个模块自己声明的 name/description 文案键，都要在两个目录里存在。

    这里**必须遍历磁盘上的模块**，不能手写列表：手写的那版只覆盖了 2 个模块，
    另外 5 个的文案键从来没被检查过（`test_cog_registry_covers_every_module`
    是同一类问题的另一道闸）。
    """
    module_ids = sorted(
        path.name for path in MODULES_DIR.iterdir() if path.is_dir() and (path / "__init__.py").is_file()
    )
    assert module_ids, "一个模块都没发现，这条测试自己失效了"

    missing: list[str] = []
    for module_id in module_ids:
        meta = importlib.import_module(f"bot.modules.{module_id}").MODULE_META
        for key in (meta.name_key, meta.description_key):
            for locale in ("zh-CN", "en-US"):
                if key not in catalogs[locale]:
                    missing.append(f"{module_id}: {locale} 缺少 {key}")

    assert missing == []


def test_status_label_keys_exist(catalogs: dict[str, dict[str, str]]) -> None:
    from bot.core.framework import STATUS_LABEL_KEY

    for key in STATUS_LABEL_KEY.values():
        assert key in catalogs["en-US"], key
        assert key in catalogs["zh-CN"], key


# ---------------------------------------------------------------- 命令元数据


def test_command_names_are_not_localized(catalogs: dict[str, dict[str, str]]) -> None:
    """命令名保持英文：不提供任何 commands.*.name 文案。"""
    offenders = sorted(
        key for catalog in catalogs.values() for key in catalog if key.startswith("commands.") and key.endswith(".name")
    )

    assert offenders == []


def test_localized_descriptions_are_not_too_long(catalogs: dict[str, dict[str, str]]) -> None:
    too_long = [
        key
        for catalog in catalogs.values()
        for key, value in catalog.items()
        if key.startswith("commands.") and key.endswith(".description") and len(value) > 100
    ]

    assert too_long == []


# 「每条命令都有中文描述」这一条在 tests/test_real_bot.py 里查真正会发给 Discord 的 payload——
# 那比按命令名拼 key 再翻目录更可靠，也不依赖任何命名约定（连字符命令名就会踩这个坑）。


# ---------------------------------------------------------------- 声明与实现一致


def check_passes(command: app_commands.Command, permissions: discord.Permissions) -> bool:
    interaction = SimpleNamespace(permissions=permissions, guild=SimpleNamespace(id=1))
    for check in command.checks:
        try:
            result = check(interaction)
        except Exception:
            return False
        if result is False:
            return False
    return True


def test_every_checked_command_declares_why() -> None:
    """带检查的命令必须声明权限标签或 owner_only，否则 /help 说不清「为什么不能用」。"""
    undeclared = sorted(
        command.qualified_name
        for command in all_commands()
        if command.checks
        and not ((command.extras or {}).get("permissions") or (command.extras or {}).get("owner_only"))
    )

    assert undeclared == []


def test_declared_permissions_match_the_enforced_checks() -> None:
    """``/help`` 展示的权限要求必须和真正生效的检查一致——这条能抓住两边漂移。"""
    problems: list[str] = []
    for command in all_commands():
        declared = tuple((command.extras or {}).get("permissions") or ())
        if not declared:
            continue
        label = command.qualified_name

        if not check_passes(command, discord.Permissions(**{flag: True for flag in declared})):
            problems.append(f"{label}：声明了 {declared}，但全都给上时检查仍然失败")

        for dropped in declared:
            remaining = {flag: True for flag in declared if flag != dropped}
            if check_passes(command, discord.Permissions(**remaining)):
                problems.append(f"{label}：少了 {dropped} 却仍然通过检查")

    assert problems == []
