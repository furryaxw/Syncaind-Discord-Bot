"""forms：表单 —— 申请、收集、报名用的是同一套东西。

* 表单是**数据**，而且**不在仓库里**：定义放在数据目录的 ``forms/`` 下（默认 ``data/forms/``，
  与数据库、附件同一个卷），一个文件一个表单，文字也写在那个文件里。
  改完 ``/module reload forms`` 生效，仓库里不会出现你的表单内容。
* 卡片的按钮、审核按钮都**不带状态**：点进来按消息 id 反查是哪一个发布 / 哪一条提交。
* 附件一提交就下载进 ``data/attachments/``（Discord 的下载链接会过期），草稿过期时连目录一起清掉。
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from bot.core.module import ModuleMeta

MODULE_META = ModuleMeta(
    id="forms",
    default_enabled=True,
    # 发布表单、绑定频道与角色、查结果都改服务器配置；填写与提交不需要任何服务器权限。
    required_permissions=("manage_guild",),
)


async def setup(bot: Any) -> None:
    from . import schema
    from .cog import FormOpenView, FormReviewView, FormsCog
    from .store import build_stores

    logger = bot.log.getChild("forms")
    data_dir = Path(bot.settings.database_path).parent
    definitions_dir = data_dir / "forms"
    catalog = schema.load_catalog(definitions_dir, logger=logger)

    stores = build_stores(bot.db, logger=logger)
    cog = FormsCog(
        bot,
        stores,
        catalog=catalog,
        definitions_dir=definitions_dir,
        attachments_root=data_dir / "attachments",
    )
    await bot.add_cog(cog)
    # 旧子命令不再注册；保留 FormsCog 的方法给 web 审核、key 联动与面板调用。
    bot.tree.remove_command("form")
    from .panel import FormPanelCog

    await bot.add_cog(FormPanelCog(bot, cog, stores, catalog))
    # 持久化按钮：注册一次，重启后旧卡片上的按钮照样能点
    # （状态按消息 id 反查，所以不需要每张卡片各注册一个 view）。
    bot.add_view(FormOpenView(cog))
    bot.add_view(FormReviewView(cog))
    cog.start_sweeping()

    from .key_link import FormKeyLinker

    linker = FormKeyLinker(bot, logger=logger)
    bot.services.unregister("form_keys")
    bot.services.register("form_keys", linker)

    from .review import FormReviewApp
    from .token_store import FormTokenStore
    from .webapp import FormWebApp

    if bot.settings.public_base_url and bot.settings.form_token_secret:
        token_store = FormTokenStore(bot.db, logger=logger)
        webapp = FormWebApp(
            bot=bot,
            catalog=catalog,
            stores=stores,
            attachments_root=data_dir / "attachments",
            definitions_dir=definitions_dir,
            token_store=token_store,
            secret=bot.settings.form_token_secret,
            base_url=bot.settings.public_base_url,
        )
        webapp.register(bot.web)
        review_app = FormReviewApp(
            bot=bot,
            catalog=catalog,
            stores=stores,
            attachments_root=data_dir / "attachments",
            token_store=token_store,
            secret=bot.settings.form_token_secret,
        )
        review_app.register(bot.web)
    else:
        logger.info(
            "HTML 表单未启用：需要 PUBLIC_BASE_URL 与 FORM_TOKEN_SECRET；/form 仍用旧交互"
        )
