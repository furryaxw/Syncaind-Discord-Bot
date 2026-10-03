"""命令元数据的本地化：把斜杠命令的名称、描述与参数描述接到同一套文案目录上。

discord.py 2.x 的命令元数据本地化走 :class:`app_commands.Translator`，而不是直接传
``name_localizations``。走这条路的好处是：命令元数据与运行时文案共用 ``locales/*.json``，
不会出现「界面中文、命令英文」这种两套语言的状态。

用法是 ``localized("Kick a member from the server", "commands.kick.description")``：
第一个参数是英文默认值（Discord 也要求命令名的 message 必须是这个名字本身），第二个是文案键。

**只翻译描述与参数说明，不翻译命令名。** 命令名的国际化会让肌肉记忆和外部脚本失效，
而 localized 名称一旦不合 Discord 的命名规则还会让整次 sync 失败。名字保持英文，
描述按客户端语言切换。
"""

from __future__ import annotations

import logging

import discord
from discord import app_commands

from .i18n import I18n

KEY_EXTRA = "key"


def localized(value: str, key: str) -> app_commands.locale_str:
    """标记一个待翻译的命令元数据字符串。"""
    return app_commands.locale_str(value, **{KEY_EXTRA: key})


class CatalogTranslator(app_commands.Translator):
    """从 :class:`I18n` 的目录里取翻译。"""

    def __init__(self, i18n: I18n, logger: logging.Logger | None = None) -> None:
        self._i18n = i18n
        self._logger = logger or logging.getLogger("bot.i18n")

    async def translate(
        self,
        string: app_commands.locale_str,
        locale: discord.Locale,
        context: app_commands.TranslationContext,
    ) -> str | None:
        """返回 ``None`` 表示「保持英文原文」。"""
        key = string.extras.get(KEY_EXTRA)
        if not isinstance(key, str):
            # auto_locale_strings 会把没绑 key 的字符串也包起来，那些保持英文。
            return None
        translated = self._i18n.lookup(locale, key)
        if translated is None:
            self._logger.warning("命令元数据缺少文案 %s（语言 %s）", key, locale)
        return translated
