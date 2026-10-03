"""Bot 装配：把配置、日志、i18n、数据层、模块加载器与框架命令接起来。"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

import discord
from discord.ext import commands

from .config import Settings
from .database import Database
from .errors import register_error_handler
from .framework import FrameworkCog
from .help import HelpCog
from .i18n import I18n, discover_module_locales
from .loader import ModuleLoader
from .store import GuildSettingsStore, ModuleStateStore
from .translator import CatalogTranslator
from .web import BotWebServer

CORE_DIR = Path(__file__).resolve().parent
BOT_DIR = CORE_DIR.parent
MODULES_DIR = BOT_DIR / "modules"
LOCALES_DIR = BOT_DIR / "locales"


class SyncaindBot(commands.Bot):
    """单服务器部署的机器人。"""

    def __init__(self, *, settings: Settings, logger: logging.Logger | None = None) -> None:
        intents = discord.Intents.default()
        # 角色层级比对与 /userinfo 都需要成员数据，这是必须开启的特权 intent。
        intents.members = True
        super().__init__(
            command_prefix=commands.when_mentioned,
            intents=intents,
            help_command=None,
        )
        self.settings = settings
        self.log = logger or logging.getLogger("bot")
        self.i18n = I18n(
            LOCALES_DIR,
            settings.default_locale,
            self.log.getChild("i18n"),
            # 每个模块自带的 locales/ 一并合进来，模块的文案随模块一起增删。
            extra_sources=discover_module_locales(MODULES_DIR),
        )
        self.db = Database(settings.database_path, logger=self.log.getChild("db"))
        self.guild_settings = GuildSettingsStore(
            self.db,
            default_mod_log_channel_id=settings.mod_log_channel_id,
            logger=self.log.getChild("settings"),
        )
        self.module_state = ModuleStateStore(self.db)
        self.web = BotWebServer(
            host=settings.web_host,
            port=settings.web_port,
            logger=self.log.getChild("web"),
        )
        self.loader = ModuleLoader(
            self,
            guild_id=settings.guild_id,
            modules_dir=MODULES_DIR,
            state_store=self.module_state,
            logger=self.log.getChild("loader"),
        )

    # ------------------------------------------------------------------ 便捷方法

    def t(self, locale_source: Any, key: str, **kwargs: Any) -> str:
        """取文案。``locale_source`` 可以是 interaction、``discord.Locale`` 或语言标签。"""
        return self.i18n.t(getattr(locale_source, "locale", locale_source), key, **kwargs)

    @property
    def guild_object(self) -> discord.Object:
        return discord.Object(id=self.settings.guild_id)

    # ------------------------------------------------------------------ 生命周期

    async def setup_hook(self) -> None:
        await self.prepare()
        await self.sync_commands()

    async def prepare(self) -> None:
        """装配中除「同步命令」以外的所有步骤。

        单独拆出来是为了让测试与生产走**同一条路径**（自己拼装容易漏掉
        ``set_translator`` 这类步骤，测出来的就不是真实行为）。
        于是命令行得是英文却没人发现。凡是装配顺序的知识，只允许在这里存在一份。
        """
        await self.db.connect()
        applied = await self.db.migrate()
        if applied:
            self.log.info("数据库迁移已应用：%s", applied)

        register_error_handler(self.tree, i18n=self.i18n, logger=self.log.getChild("errors"))
        # 必须在任何一次 sync 之前设好，否则命令描述不会被翻译。
        await self.tree.set_translator(CatalogTranslator(self.i18n, self.log.getChild("i18n")))
        await self.add_framework_cogs()
        await self.loader.load_all()
        # 模块在 setup 里注册入站路由，所以端点必须等它们都加载完再起。
        await self.web.start()

    async def add_framework_cogs(self) -> None:
        """挂上框架自带的 Cog。

        它们先于业务模块加载：即使所有模块都坏掉，``/module`` 与 ``/help`` 也必须能用。
        测试也走这个方法，免得「哪些框架 Cog 必须存在」这件事出现两份说法。
        """
        await self.add_cog(FrameworkCog(self))
        await self.add_cog(HelpCog(self))

    async def sync_commands(self) -> int:
        """把命令按 guild 级同步（改动即时生效）。"""
        guild = self.guild_object
        self.tree.copy_global_to(guild=guild)
        synced = await self.tree.sync(guild=guild)
        self.log.info("已同步 %d 个斜杠命令到 guild %s", len(synced), self.settings.guild_id)
        return len(synced)

    async def close(self) -> None:
        await self.web.stop()
        await self.db.close()
        await super().close()

    async def on_ready(self) -> None:
        guild = self.get_guild(self.settings.guild_id)
        if guild is None:
            self.log.error(
                "机器人不在 GUILD_ID=%s 这个服务器里，命令不会出现。请先邀请它加入。",
                self.settings.guild_id,
            )
            return
        self.log.info(
            "已连接：%s（%s 位成员，加载了 %d 个模块）",
            guild.name,
            guild.member_count,
            len(self.loader.loaded_ids),
        )

    async def on_app_command_completion(
        self,
        interaction: discord.Interaction,
        command: Any,
    ) -> None:
        """每条命令跑完都留一条记录。

        没有这条日志时，「命令没被派发」「跑了但没回应」「跑了并回应了」三种情况在日志里
        长得一模一样——排查「该交互失败」时会完全抓瞎。这里顺便记下从交互创建到完成的耗时，
        它才是和 Discord 那 3 秒窗口直接对应的数字。
        """
        elapsed = discord.utils.utcnow() - interaction.created_at
        self.log.info(
            "命令 /%s 已完成（用户 %s，从交互创建起 %.0f ms）",
            getattr(command, "qualified_name", getattr(command, "name", "?")),
            interaction.user.id,
            elapsed.total_seconds() * 1000,
        )
