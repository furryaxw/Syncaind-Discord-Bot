"""命令元数据翻译：Translator 从同一套文案目录取值。

注意命令名**不参与**翻译——只有描述与参数说明会走这里。
"""

from __future__ import annotations

import discord
from discord import app_commands

from bot.core.i18n import I18n
from bot.core.translator import CatalogTranslator, localized

DESCRIPTION = "Kick a member from the server"


async def test_chinese_client_gets_chinese_metadata(i18n: I18n) -> None:
    translator = CatalogTranslator(i18n)

    translated = await translator.translate(
        localized(DESCRIPTION, "commands.kick.description"), discord.Locale.chinese, None
    )

    assert translated == "把成员踢出服务器"


async def test_english_client_gets_english_metadata(i18n: I18n) -> None:
    translator = CatalogTranslator(i18n)

    translated = await translator.translate(
        localized(DESCRIPTION, "commands.kick.description"), discord.Locale.american_english, None
    )

    assert translated == DESCRIPTION


async def test_other_locales_fall_back_to_english(i18n: I18n) -> None:
    translator = CatalogTranslator(i18n)

    translated = await translator.translate(
        localized(DESCRIPTION, "commands.kick.description"), discord.Locale.french, None
    )

    assert translated == DESCRIPTION


async def test_unknown_key_returns_none_so_discord_keeps_the_default(i18n: I18n) -> None:
    translator = CatalogTranslator(i18n)

    translated = await translator.translate(
        localized(DESCRIPTION, "commands.does_not_exist.description"), discord.Locale.chinese, None
    )

    assert translated is None


async def test_strings_without_a_key_are_left_alone(i18n: I18n) -> None:
    """``auto_locale_strings`` 会把没绑 key 的字符串也包起来，那些必须保持原样。

    命令名就靠这条路径保持英文。
    """
    translator = CatalogTranslator(i18n)

    translated = await translator.translate(app_commands.locale_str("kick"), discord.Locale.chinese, None)

    assert translated is None
