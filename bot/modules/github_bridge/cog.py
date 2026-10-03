"""github_bridge：把 Discord 用户绑到 GitHub 账号（`/link` 一组）。

这是 GitHub 集成的**核心**：通知、身份联动、以及「谁能触发仓库操作」都建立在这张映射表上。

两种授权方式，按配置自动选：

* **web flow**（网页授权）：点链接 → 浏览器点一下授权 → 回调到机器人的入站端点。
  体验最顺，但需要 ``client_secret`` + 一个公网可达的回调地址。
* **设备流**：手输一次性码。唯一不需要公网地址的方式，作为兜底。

**只存身份，不存 token**：映射表只需要「谁是谁」，检查组织成员身份用机器人自己的凭据。
"""

from __future__ import annotations

import asyncio
import logging
from typing import Any, Literal
from urllib.parse import urlparse

import aiohttp
import discord
from aiohttp import web
from discord import app_commands
from discord.ext import commands

from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import info_embed, ok_embed, warn_embed
from bot.core.web import minimal_page
from bot.integrations.github import (
    STATUS_DONE,
    STATUS_SLOW_DOWN,
    AiohttpTransport,
    DeviceCode,
    DeviceFlowError,
    GitHubAccountConflict,
    GitHubAccountStore,
    GitHubClient,
    StateStore,
    Transport,
    WebFlowError,
)

LOGGER = logging.getLogger("bot.github")

LinkMethod = Literal["auto", "web", "device"]


@app_commands.guild_only()
class GitHubBridgeCog(commands.Cog):
    """GitHub ↔ Discord 账号映射。"""

    link_group = app_commands.Group(
        name="link",
        description=localized("Link your accounts", "commands.link.description"),
    )

    def __init__(
        self,
        bot: Any,
        store: GitHubAccountStore,
        *,
        transport: Transport | None = None,
    ) -> None:
        self.bot = bot
        self.store = store
        self._transport = transport
        self._session: aiohttp.ClientSession | None = None
        self._states = StateStore()
        # 后台轮询任务必须留引用：asyncio 文档明确说任务可能被 GC 掉，
        # 而这个任务要跑好几分钟。
        self._tasks: set[asyncio.Task[None]] = set()
        self._callback_path = _path_of(bot.settings.github_oauth_redirect_uri)
        if self._callback_path is not None:
            try:
                bot.web.add_route("GET", self._callback_path, self._handle_callback)
            except ValueError:
                LOGGER.warning("回调路由 %s 已被占用，web flow 会失败", self._callback_path)

    async def cog_unload(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        if self._callback_path is not None:
            self.bot.web.remove_route("GET", self._callback_path)
        if self._session is not None and not self._session.closed:
            await self._session.close()

    # ------------------------------------------------------------------ /link github

    @link_group.command(
        name="github",
        description=localized(
            "Link your GitHub account with a one-time code",
            "commands.link.github.description",
        ),
    )
    @app_commands.describe(
        method=localized(
            "How to authorise; auto picks the smoothest one that is configured",
            "commands.link.github.param_method",
        )
    )
    async def link_github(
        self,
        interaction: discord.Interaction,
        method: LinkMethod = "auto",
    ) -> None:
        guild = self._require_guild(interaction)
        client = self._client()

        if self._use_web_flow(method):
            await self._start_web_flow(interaction, guild.id, client)
            return

        try:
            flow = await client.start_device_flow()
        except (DeviceFlowError, aiohttp.ClientError) as exc:
            LOGGER.exception("开始 GitHub 设备流失败")
            raise UserError("github.link.start_failed", error=f"{type(exc).__name__}: {exc}") from exc

        t = self.bot.t
        await interaction.response.send_message(
            embed=info_embed(
                t(interaction, "github.link.title"),
                t(
                    interaction,
                    "github.link.instructions",
                    code=f"`{flow.user_code}`",
                    url=flow.verification_uri,
                    minutes=flow.expires_in // 60,
                ),
            ),
            ephemeral=True,
        )

        # 轮询要跑好几分钟，不能占着交互：先把码发出去，再在后台等。
        task = asyncio.create_task(self._await_login(interaction, guild.id, flow, client))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ------------------------------------------------------------------ web flow

    def _use_web_flow(self, method: LinkMethod) -> bool:
        if method == "device":
            return False
        if method == "web":
            if not self._client().can_use_web_flow:
                raise UserError("github.web.not_configured")
            if not self.bot.web.is_running:
                raise UserError("github.web.endpoint_off")
            return True
        # auto：配置齐了、且端点真的在跑，才用 web flow
        return self._client().can_use_web_flow and self.bot.web.is_running

    async def _start_web_flow(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        client: GitHubClient,
    ) -> None:
        pending = self._states.create(guild_id=guild_id, discord_user_id=interaction.user.id)
        url = client.authorize_url(pending.state)

        t = self.bot.t
        view = discord.ui.View(timeout=None)
        view.add_item(discord.ui.Button(label=t(interaction, "github.web.button"), url=url))
        await interaction.response.send_message(
            embed=warn_embed(
                t(interaction, "github.link.title"),
                t(interaction, "github.web.instructions", url=url, minutes=self._states_ttl_minutes),
            ),
            view=view,
            ephemeral=True,
        )

    @property
    def _states_ttl_minutes(self) -> int:
        from bot.integrations.github import DEFAULT_TTL_SECONDS

        return max(1, DEFAULT_TTL_SECONDS // 60)

    async def _handle_callback(self, request: web.Request) -> web.StreamResponse:
        """GitHub 授权完把浏览器踢回这里。

        这里的一切都不可信：``state`` 一次性且会过期，``code`` 只在服务端换 token，
        页面里出现的文字全部来自我们自己的文案目录（不拼请求参数）。
        """
        pending = self._states.consume(request.query.get("state", ""))
        if pending is None:
            return self._page(request, 400, "github.web.bad_state_title", "github.web.bad_state_body")

        denied = request.query.get("error")
        if denied:
            LOGGER.info("用户拒绝了 GitHub 授权：%s", denied)
            await self._notify(pending, "github.link.failed", ok=False, error=str(denied))
            return self._page(request, 200, "github.web.denied_title", "github.web.denied_body")

        code = request.query.get("code")
        if not code:
            return self._page(request, 400, "github.web.bad_state_title", "github.web.bad_state_body")

        try:
            user = await self._client().exchange_code(code)
        except (WebFlowError, aiohttp.ClientError) as exc:
            LOGGER.exception("web flow 换取 token 失败")
            await self._notify(
                pending, "github.link.failed", ok=False, error=str(getattr(exc, "code", type(exc).__name__))
            )
            return self._page(request, 200, "github.failed_title", "github.web.exchange_failed_body")

        try:
            account = await self.store.link(
                pending.guild_id,
                discord_user_id=pending.discord_user_id,
                github_user_id=user.id,
                github_login=user.login,
            )
        except GitHubAccountConflict as exc:
            await self._notify(
                pending,
                "github.link.conflict",
                ok=False,
                login=exc.github_login,
                holder=f"<@{exc.holder_discord_user_id}>",
            )
            return self._page(request, 200, "github.failed_title", "github.web.conflict_body")

        await self._notify(pending, "github.link.done", ok=True, login=account.github_login)
        return self._page(
            request, 200, "github.web.success_title", "github.web.success_body", login=account.github_login
        )

    async def _notify(
        self,
        pending: Any,
        key: str,
        *,
        ok: bool,
        **kwargs: Any,
    ) -> None:
        """回调是从浏览器来的，手上没有交互对象，所以只能私信当事人。"""
        guild = self.bot.get_guild(pending.guild_id)
        member = guild.get_member(pending.discord_user_id) if guild is not None else None
        if member is None:
            LOGGER.warning("绑定结果无法私信：找不到成员 %s", pending.discord_user_id)
            return
        settings = await self.bot.guild_settings.get(pending.guild_id)
        text = self.bot.i18n.t(settings.locale, key, guild=guild.name, **kwargs)
        try:
            await member.send(text)
        except discord.HTTPException:
            LOGGER.debug("无法私信 %s（可能关闭了私信）；浏览器那一页已经说明了结果", member.id)

    async def _await_login(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        flow: DeviceCode,
        client: GitHubClient,
    ) -> None:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + flow.expires_in
        interval = flow.interval

        while loop.time() < deadline:
            await asyncio.sleep(interval)
            try:
                result = await client.poll_device_flow(flow.device_code)
            except (DeviceFlowError, aiohttp.ClientError) as exc:
                LOGGER.warning("设备流轮询失败：%s", exc)
                await self._finish(
                    interaction,
                    "github.link.failed",
                    error=str(getattr(exc, "code", type(exc).__name__)),
                    ok=False,
                )
                return

            if result.status == STATUS_SLOW_DOWN:
                interval = result.interval or interval
                continue
            if result.status != STATUS_DONE or result.user is None:
                continue

            await self._bind(interaction, guild_id, result.user.id, result.user.login)
            return

        await self._finish(interaction, "github.link.expired", ok=False)

    async def _bind(
        self,
        interaction: discord.Interaction,
        guild_id: int,
        github_user_id: int,
        github_login: str,
    ) -> None:
        try:
            account = await self.store.link(
                guild_id,
                discord_user_id=interaction.user.id,
                github_user_id=github_user_id,
                github_login=github_login,
            )
        except GitHubAccountConflict as exc:
            await self._finish(
                interaction,
                "github.link.conflict",
                login=exc.github_login,
                holder=f"<@{exc.holder_discord_user_id}>",
                ok=False,
            )
            return
        await self._finish(interaction, "github.link.done", login=account.github_login, ok=True)

    async def _finish(self, interaction: discord.Interaction, key: str, *, ok: bool, **kwargs: Any) -> None:
        t = self.bot.t
        title = t(interaction, "github.done_title" if ok else "github.failed_title")
        text = t(interaction, key, **kwargs)
        embed = ok_embed(title, text) if ok else info_embed(title, text)
        await self._edit(interaction, embed)

    @staticmethod
    async def _edit(interaction: discord.Interaction, embed: discord.Embed) -> None:
        try:
            await interaction.edit_original_response(embed=embed, view=None)
        except discord.HTTPException:
            # 交互令牌可能已经过期（比如用户很晚才完成授权）。结果已落库，日志里也有记录。
            LOGGER.warning("无法更新绑定结果（交互已过期）：user=%s", interaction.user.id)

    # ------------------------------------------------------------------ /link show

    @link_group.command(
        name="show",
        description=localized("Show the GitHub account someone linked", "commands.link.show.description"),
    )
    @app_commands.describe(member=localized("Whose binding to show (defaults to you)", "commands.link.param_member"))
    async def link_show(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = self._resolve_target(interaction, member)
        account = await self.store.get(interaction.guild.id, target.id)  # type: ignore[union-attr]
        t = self.bot.t
        title = t(interaction, "github.show.title", member=target.display_name)
        if account is None:
            await interaction.response.send_message(
                embed=info_embed(title, t(interaction, "github.show.empty")), ephemeral=True
            )
            return

        await interaction.response.send_message(
            embed=info_embed(
                title,
                t(
                    interaction,
                    "github.show.value",
                    login=account.github_login,
                    id=account.github_user_id,
                    time=account.linked_at,
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /link remove

    @link_group.command(
        name="remove",
        description=localized("Remove a GitHub binding", "commands.link.remove.description"),
    )
    @app_commands.describe(member=localized("Whose binding to remove (defaults to you)", "commands.link.param_member"))
    async def link_remove(
        self,
        interaction: discord.Interaction,
        member: discord.Member | None = None,
    ) -> None:
        target = self._resolve_target(interaction, member)
        removed = await self.store.unlink(interaction.guild.id, target.id)  # type: ignore[union-attr]
        if not removed:
            raise UserError("github.remove.not_linked", member=target.display_name)

        t = self.bot.t
        await interaction.response.send_message(
            embed=ok_embed(
                t(interaction, "github.remove.done_title"),
                t(interaction, "github.remove.done", member=target.display_name),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 内部

    def _resolve_target(self, interaction: discord.Interaction, member: discord.Member | None) -> discord.Member:
        """看/改别人的绑定时要求 Manage Roles——GitHub 用户名本身也算一种个人信息。"""
        guild = self._require_guild(interaction)
        target = member or guild.get_member(interaction.user.id)
        if target is None:
            raise UserError("errors.member_not_found")
        if target.id != interaction.user.id and not getattr(interaction.permissions, "manage_roles", False):
            raise UserError(
                "errors.missing_permissions",
                permissions=self.bot.t(interaction, "permissions.manage_roles"),
            )
        return target

    def _client(self) -> GitHubClient:
        client_id = self.bot.settings.github_oauth_client_id
        if not client_id:
            raise UserError("github.not_configured")
        if self._transport is None:
            if self._session is None or self._session.closed:
                self._session = aiohttp.ClientSession()
            self._transport = AiohttpTransport(self._session)
        return GitHubClient(
            client_id,
            self._transport,
            client_secret=self.bot.settings.github_oauth_client_secret,
            redirect_uri=self.bot.settings.github_oauth_redirect_uri,
            logger=self.bot.log.getChild("github"),
        )

    def _page(
        self,
        request: web.Request,
        status: int,
        title_key: str,
        body_key: str,
        **kwargs: Any,
    ) -> web.Response:
        """给浏览器的那一页。

        语言按浏览器的 ``Accept-Language`` 选（Discord 的客户端语言在这里拿不到，
        HTTP 头是浏览器场景下最接近的信号）；匹配不上就用默认语言。
        文字全部来自文案目录，绝不拼请求参数——``minimal_page`` 还会再转义一次。
        """
        locale = self._page_locale(request)
        title = self.bot.i18n.t(locale, title_key)
        body = self.bot.i18n.t(locale, body_key, **kwargs)
        return web.Response(status=status, text=minimal_page(title, body), content_type="text/html")

    def _page_locale(self, request: web.Request) -> str:
        default = self.bot.i18n.default_locale
        for chunk in request.headers.get("Accept-Language", "").split(","):
            tag = chunk.split(";")[0].strip()
            if not tag or tag == "*":
                continue
            resolved = self.bot.i18n.resolve(tag)
            if resolved != default or tag.lower().startswith("en"):
                return resolved
        return default

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild


def _path_of(uri: str | None) -> str | None:
    """从回调地址里取出路径——它必须和注册进来的路由一致。

    用回调地址推导路径而不是再加一个配置项：两者不一致是这类集成最常见的错配，
    推导出来就不可能对不上。
    """
    if not uri:
        return None
    path = urlparse(uri).path
    return path or "/"
