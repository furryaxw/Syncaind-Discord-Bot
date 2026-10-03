"""真装配测试：用真正的 SyncaindBot 加载真正的模块，验证命令树。

这一层不做网络请求（不启动、不 sync），但它走的是真实的装配代码路径，
因此 app_commands 的装饰器、Range 参数、Group、guild_only 一旦写错就会在这里暴露。
"""

from __future__ import annotations

import logging
from pathlib import Path

from bot.core.bot import SyncaindBot
from bot.core.loader import ModuleLoader
from bot.core.module import ModuleStatus
from bot.core.translator import CatalogTranslator
from tests.conftest import GUILD_ID
from tests.discord_fakes import MODERATOR_ID, FakeInteraction, build_guild, setup_bot

EXPECTED_COMMANDS = {
    "help",
    "module",
    "kick",
    "ban",
    "unban",
    "timeout",
    "warn",
    "purge",
    "history",
    "case",
    "channel",
    "role",
    "reactionrole",
    "link",
    "feed",
    "key",
    "serverinfo",
    "userinfo",
    "permissions",
}


async def make_bot(settings) -> SyncaindBot:
    bot = SyncaindBot(settings=settings)
    await bot.db.connect()
    await bot.db.migrate()
    return bot


async def test_real_modules_load_and_register_every_command(settings) -> None:
    bot = await make_bot(settings)
    try:
        await bot.add_framework_cogs()
        await bot.loader.load_all()

        statuses = {info.id: info.status for info in bot.loader.infos()}
        command_names = {command.name for command in bot.tree.get_commands()}
    finally:
        await bot.db.close()

    assert statuses.keys() == set(bot.loader.discover()), "有模块没被加载，也没被记录状态"
    assert set(statuses.values()) == {ModuleStatus.LOADED}
    assert EXPECTED_COMMANDS <= command_names


async def test_module_group_has_the_four_management_subcommands(settings) -> None:
    bot = await make_bot(settings)
    try:
        await bot.add_framework_cogs()
        await bot.loader.load_all()

        group = next(command for command in bot.tree.get_commands() if command.name == "module")
        subcommands = {child.name for child in group.commands}  # type: ignore[attr-defined]
    finally:
        await bot.db.close()

    assert subcommands == {"list", "enable", "disable", "reload"}


async def test_framework_commands_survive_a_world_with_no_modules(settings, tmp_path: Path) -> None:
    """框架命令必须先于业务模块挂上，否则「所有模块都坏了」时就无法补救。"""
    bot = await make_bot(settings)
    try:
        bot.loader = ModuleLoader(
            bot,
            guild_id=GUILD_ID,
            modules_dir=tmp_path / "no_modules_here",
            state_store=bot.module_state,
        )
        await bot.add_framework_cogs()
        await bot.loader.load_all()

        command_names = {command.name for command in bot.tree.get_commands()}
        statuses = bot.loader.infos()
    finally:
        await bot.db.close()

    assert statuses == []
    assert {"module", "help"} <= command_names


async def test_disabling_a_module_removes_its_commands(settings) -> None:
    bot = await make_bot(settings)
    try:
        await bot.add_framework_cogs()
        await bot.loader.load_all()
        assert "kick" in {command.name for command in bot.tree.get_commands()}

        await bot.loader.set_enabled("moderation", False)

        command_names = {command.name for command in bot.tree.get_commands()}
    finally:
        await bot.db.close()

    assert "kick" not in command_names
    assert "ban" not in command_names
    assert "serverinfo" in command_names


async def test_setup_hook_wires_everything_up(settings, monkeypatch) -> None:
    """整条装配链（连库、迁移、错误处理、翻译器、框架命令、模块、同步）离线跑通。

    只把联网的那一步（``sync_commands``）换成桩——它是唯一必须连 Discord 的环节。
    """
    bot = SyncaindBot(settings=settings)
    sync_calls: list[int] = []

    async def fake_sync() -> int:
        sync_calls.append(1)
        return 10

    monkeypatch.setattr(bot, "sync_commands", fake_sync)

    try:
        await bot.setup_hook()

        statuses = {info.id: info.status for info in bot.loader.infos()}
        command_names = {command.name for command in bot.tree.get_commands()}
        has_error_handler = bot.tree.on_error is not None
        translator = bot.tree.translator
        framework_registered = {"FrameworkCog", "HelpCog"} <= set(bot.cogs)
        source_labels = {source.label for source in bot.i18n.sources}
    finally:
        await bot.db.close()

    assert sync_calls == [1]
    assert statuses.keys() == set(bot.loader.discover()), "有模块没被加载，也没被记录状态"
    assert set(statuses.values()) == {ModuleStatus.LOADED}
    assert EXPECTED_COMMANDS <= command_names
    assert has_error_handler
    assert isinstance(translator, CatalogTranslator)
    assert framework_registered
    # 模块自带的文案目录确实被合并进来了
    assert {"core", "moderation", "tools"} <= source_labels


async def test_sync_payload_localizes_descriptions_but_not_names(settings) -> None:
    """真正会被发给 Discord 的那份 payload：描述与参数说明有中文，命令名保持英文。"""
    bot = await make_bot(settings)
    try:
        await bot.tree.set_translator(CatalogTranslator(bot.i18n))
        await bot.add_framework_cogs()
        await bot.loader.load_all()

        kick = next(command for command in bot.tree.get_commands() if command.name == "kick")
        payload = await kick.get_translated_payload(bot.tree, bot.tree.translator)
    finally:
        await bot.db.close()

    assert payload["name"] == "kick"
    assert not payload.get("name_localizations"), "命令名不应该被本地化"
    assert payload["description_localizations"]["zh-CN"] == "把成员踢出服务器"
    option = payload["options"][0]
    assert option["name"] == "member"
    assert option["description_localizations"]["zh-CN"] == "要踢出的成员"


async def test_no_command_name_is_localized(settings) -> None:
    bot = await make_bot(settings)
    try:
        await bot.tree.set_translator(CatalogTranslator(bot.i18n))
        await bot.add_framework_cogs()
        await bot.loader.load_all()

        localized_names = {}
        for command in bot.tree.walk_commands():
            payload = await command.get_translated_payload(bot.tree, bot.tree.translator)
            if payload.get("name_localizations"):
                localized_names[command.qualified_name] = payload["name_localizations"]
    finally:
        await bot.db.close()

    assert localized_names == {}


async def test_group_payload_keeps_english_names_too(settings) -> None:
    bot = await make_bot(settings)
    try:
        await bot.tree.set_translator(CatalogTranslator(bot.i18n))
        await bot.add_framework_cogs()
        await bot.loader.load_all()

        group = next(command for command in bot.tree.get_commands() if command.name == "module")
        payload = await group.get_translated_payload(bot.tree, bot.tree.translator)
    finally:
        await bot.db.close()

    assert payload["name"] == "module"
    assert not payload.get("name_localizations")
    assert payload["description_localizations"]["zh-CN"] == "管理机器人的功能模块"
    assert {child["name"] for child in payload["options"]} == {"list", "enable", "disable", "reload"}


async def test_every_command_has_a_localized_description(settings) -> None:
    """直接看会发给 Discord 的那份 payload：每条命令（含分组）都该带上中文描述。

    这比「按命令名拼出文案键再去翻目录」可靠得多——不依赖任何命名约定，
    而且自动覆盖所有被加载的模块。连字符命令名（``channel overwrite-category``）
    就会让「拼键」那套误报。
    """
    bot = await setup_bot(settings)
    try:
        translator = bot.tree.translator
        missing: list[str] = []
        for command in bot.tree.walk_commands():
            payload = await command.get_translated_payload(bot.tree, translator)
            if not payload.get("description_localizations", {}).get("zh-CN"):
                missing.append(command.qualified_name)
    finally:
        await bot.db.close()

    assert missing == []


async def test_loading_the_real_modules_warns_about_nothing(settings, caplog) -> None:
    """用真实模块跑一遍：能加载的都加载了，所以日志里不该有任何 WARNING。

    真实模块里 ``cases`` 依赖 ``moderation`` 且字母序排在它**前面** —— 它正是当年那行
    「模块 cases 未加载：依赖未加载：moderation」的来源。那只是本轮还没轮到，
    第二轮就成功了，却每次启动都打一行像故障的警告。
    """
    bot = await make_bot(settings)
    try:
        with caplog.at_level(logging.WARNING, logger="bot.loader"):
            await bot.loader.load_all()

        assert all(bot.loader.info(module_id).status is ModuleStatus.LOADED for module_id in bot.loader.discover())
    finally:
        await bot.db.close()

    assert [record.getMessage() for record in caplog.records] == []


async def test_command_completion_is_logged(settings, caplog) -> None:
    """成功执行一条命令也要留痕。

    没有这条日志时，「交互没被派发」「跑了但没回应」「跑了并回应了」三种情况在日志里
    长得一模一样——排查「该交互失败」时会完全抓瞎。
    """
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        interaction = FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild)
        command = bot.tree.get_command("help")
        assert command is not None

        with caplog.at_level(logging.INFO, logger="bot"):
            await bot.on_app_command_completion(interaction, command)
    finally:
        await bot.db.close()

    messages = [record.getMessage() for record in caplog.records]
    assert any("命令 /help 已完成" in message for message in messages)
