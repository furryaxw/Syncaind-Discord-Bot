"""access_keys：发激活码 —— 两种模式都从**按钮**进。

* **抽奖**（raffle）：带「参与」按钮 → 大家点 → 截止后随机抽 N 人 → 每人私信一枚。
* **先到先得**（fcfs）：带「领取」按钮 → 前 N 个点的人各得一枚。

**所有 SMAS 操作都要求先绑定 GitHub 账号**（`/link github`）：SMAS 的身份就是 GitHub 数字 id，
没绑就不知道该把这枚码记到谁头上。这条门禁在命令与按钮两条路上都拦。

两条实现上的关键决定：

1. **按钮状态从消息 id 反查，不存在 view 里。** 持久化 view 在启动时只注册一个实例，
   点击时 discord.py 调用的是**那个实例**的回调 —— 把 drop 存进 view 就会永远用错 drop。
   消息 id 每次交互都带，按它反查最稳，重启也不怕。
2. **按钮里的错误要自己回**：按钮不走命令树的错误处理器，抛出的 UserError 不会有人接，
   交互就会一直转圈。

发码链路每一步失败都有去处：资格 → 一人一批一枚 → **先到先得用一条 SQL 自增封顶抢名额**
（并发点击不超发）→ `take` 原子取码 → 本地登记 → 私信 → **发不出去就 `release` 退码、
撤登记、把名额还回去**。硬规则：**码只走私信，频道里永不出现完整码**。
"""

from __future__ import annotations

import asyncio
import logging
import random
from datetime import datetime, timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.clock import utcnow_iso
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import info_embed, ok_embed
from bot.integrations.github import GitHubAccount, GitHubAccountStore
from bot.integrations.smas import SYSTEM_TEAM_ID
from bot.integrations.smas.key_store import (
    CLOSED,
    DRAWN,
    FCFS,
    OPEN,
    RAFFLE,
    KeyDeliveryStore,
    KeyDeniedRoleStore,
    KeyDrop,
    KeyDropStore,
)
from bot.integrations.smas.keys import KeyPoolError, find_batch_team, release_key, take_keys

LOGGER = logging.getLogger("bot.access_keys")

TAKE_ID = "access_keys:take"
SWEEP_SECONDS = 30
DEFAULT_MINUTES = 10
MAX_KEYS = 50


def _now_plus(minutes: int) -> str:
    return (datetime.fromisoformat(utcnow_iso()) + timedelta(minutes=minutes)).isoformat(timespec="seconds")


class AccessKeysView(discord.ui.View):
    """那个按钮。**不带任何 drop 状态** —— 点进来时按消息 id 反查（见模块文档第 1 条）。"""

    def __init__(self, cog: AccessKeysCog, *, label: str | None = None) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        button = discord.ui.Button(
            style=discord.ButtonStyle.success,
            label=label or "Join",
            custom_id=TAKE_ID,
        )
        button.callback = self._on_click  # type: ignore[method-assign]
        self.add_item(button)

    async def _on_click(self, interaction: discord.Interaction) -> None:
        await self.cog.handle_button(interaction)


@app_commands.guild_only()
class AccessKeysCog(commands.Cog):
    """按批次发激活码。"""

    key_group = app_commands.Group(
        name="key",
        description=localized("Hand out activation keys", "commands.key.description"),
    )

    def __init__(
        self,
        bot: Any,
        deliveries: KeyDeliveryStore,
        drops: KeyDropStore,
        accounts: GitHubAccountStore,
        denied: KeyDeniedRoleStore,
        *,
        client: Any | None = None,
    ) -> None:
        self.bot = bot
        self.deliveries = deliveries
        self.drops = drops
        self.accounts = accounts
        self.denied = denied
        self._client = client
        self._sweeper: asyncio.Task[None] | None = None

    async def cog_unload(self) -> None:
        self.stop_sweeping()

    @property
    def configured(self) -> bool:
        return self._client is not None

    # ------------------------------------------------------------------ 语言

    async def _locale(self, guild_id: int) -> str | None:
        """公开消息、按钮标签、私信都用**服务器语言**（没配则默认）。

        命令回复用 ``interaction.locale``（那是管理员自己的客户端语言）没问题，
        但这些内容是给整个服务器看的；混用会让同一条消息前后语言不一致。
        """
        settings = await self.bot.guild_settings.get(guild_id)
        return settings.locale

    # ------------------------------------------------------------------ 门禁：先绑 GitHub

    async def _require_linked(self, interaction: discord.Interaction) -> GitHubAccount:
        """SMAS 的一切操作都要求先 `/link github` —— 没绑就不知道该记到谁头上。"""
        guild = self._require_guild(interaction)
        account = await self.accounts.get(guild.id, interaction.user.id)
        if account is None:
            raise UserError("github.link_required")
        return account

    # ------------------------------------------------------------------ 后台开奖

    def start_sweeping(self) -> bool:
        if not self.configured:
            self.bot.log.info("未配置 access server，发码相关命令会明确报未配置")
            return False
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())
        return True

    def stop_sweeping(self) -> None:
        if self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
        self._sweeper = None

    async def _sweep_loop(self) -> None:
        while True:
            try:
                await self.draw_due_drops()
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("开奖任务出错，下一轮继续")
            await asyncio.sleep(SWEEP_SECONDS)

    async def draw_due_drops(self) -> int:
        drawn = 0
        for drop in await self.drops.due(utcnow_iso()):
            try:
                await self.draw(drop)
                drawn += 1
            except Exception:
                LOGGER.exception("开奖失败：drop=%s", drop.drop_id)
        return drawn

    async def draw(self, drop: KeyDrop) -> None:
        """开奖：随机取人 → 逐个取码私信 → 公布名单（**只有人，没有码**）。"""
        pool = await self.drops.entries(drop.drop_id)
        winners = random.SystemRandom().sample(pool, min(drop.key_count, len(pool))) if pool else []
        delivered: list[int] = []
        for user_id in winners:
            user = await self._resolve_user(user_id)
            if user is not None and await self._deliver_one(drop, user):
                delivered.append(user_id)

        await self.drops.set_status(drop.drop_id, DRAWN)
        if delivered:
            mentions = "、".join(f"<@{user_id}>" for user_id in delivered)
            await self._update_drop_message(drop, closed=True, extra_key="keys.drop.winners", winners=mentions)
        else:
            await self._update_drop_message(drop, closed=True, extra_key="keys.drop.no_winners", entrants=len(pool))

    # ------------------------------------------------------------------ /key drop

    @key_group.command(
        name="drop",
        description=localized("Open a key drop: raffle or first come first served", "commands.key.drop.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(
        batch=localized("Batch id created on SMAS", "commands.key.param_batch"),
        count=localized("How many keys to hand out", "commands.key.param_count"),
        mode=localized("Raffle (button signup) or first come first served", "commands.key.param_mode"),
        role=localized("Only members with this role may take part", "commands.key.param_role"),
        minutes=localized("Raffle closes after this many minutes", "commands.key.param_minutes"),
        team=localized("Team id; looked up from the batch when omitted", "commands.key.param_team"),
    )
    @app_commands.choices(
        mode=[
            app_commands.Choice(name=localized("Raffle", "commands.key.mode.raffle"), value=RAFFLE),
            app_commands.Choice(name=localized("First come", "commands.key.mode.fcfs"), value=FCFS),
        ]
    )
    @app_commands.checks.has_permissions(manage_guild=True)
    async def key_drop(
        self,
        interaction: discord.Interaction,
        batch: str,
        count: app_commands.Range[int, 1, MAX_KEYS],
        mode: app_commands.Choice[str],
        role: discord.Role | None = None,
        minutes: app_commands.Range[int, 1, 1440] | None = None,
        team: str | None = None,
    ) -> None:
        guild = self._require_guild(interaction)
        client = self._require_client()
        await self._require_linked(interaction)
        batch_id = batch.strip()
        if not batch_id:
            raise UserError("keys.bad_batch")

        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            raise UserError("keys.bad_channel")

        await interaction.response.defer(ephemeral=True)

        team_id = team.strip() if team and team.strip() else None
        if team_id is None:
            try:
                team_id = await find_batch_team(client, batch_id=batch_id, system_team_id=SYSTEM_TEAM_ID)
            except KeyPoolError as exc:
                LOGGER.warning("查批次归属失败：batch=%s code=%s", batch_id, exc.code)
                raise UserError("keys.team_lookup_failed", batch=batch_id, error=exc.code) from exc
        if not team_id:
            raise UserError("keys.team_unknown", batch=batch_id)

        is_raffle = mode.value == RAFFLE
        closes_at = _now_plus(minutes or DEFAULT_MINUTES) if is_raffle else None
        drop = await self.drops.create(
            guild.id,
            batch_id=batch_id,
            team_id=team_id,
            channel_id=channel.id,
            mode=mode.value,
            key_count=int(count),
            role_id=role.id if role else None,
            closes_at=closes_at,
            created_by=interaction.user.id,
        )

        t = self.bot.t
        guild_locale = await self._locale(guild.id)
        message = await channel.send(
            embed=info_embed(
                self.bot.i18n.t(
                    guild_locale,
                    "keys.drop.raffle_title" if is_raffle else "keys.drop.fcfs_title",
                    batch=batch_id,
                ),
                self.bot.i18n.t(
                    guild_locale,
                    "keys.drop.raffle_body" if is_raffle else "keys.drop.fcfs_body",
                    count=int(count),
                    role=role.mention if role else self.bot.i18n.t(guild_locale, "keys.drop.anyone"),
                    minutes=minutes or DEFAULT_MINUTES,
                ),
            ),
            view=AccessKeysView(
                self,
                label=self.bot.i18n.t(guild_locale, "keys.button.enter" if is_raffle else "keys.button.claim"),
            ),
        )
        await self.drops.set_message(drop.drop_id, message.id)
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "keys.drop.created_title"),
                t(interaction, "keys.drop.created_body", count=int(count), channel=channel.mention, drop=drop.drop_id),
            )
        )

    # ------------------------------------------------------------------ 按钮

    async def handle_button(self, interaction: discord.Interaction) -> None:
        """按钮入口：按**消息 id** 找回 drop，再按它的模式分派。

        按钮不走命令树的错误处理器，所以这里必须自己把 UserError 变成一句人话，
        否则交互会一直转圈。
        """
        try:
            drop = await self.drops.get_by_message(interaction.message.id if interaction.message else 0)
            if drop is None:
                raise UserError("keys.drop.not_found")
            await self._require_linked(interaction)
            if drop.status != OPEN:
                raise UserError("keys.drop.closed")
            if drop.is_raffle:
                await self._enter(drop, interaction)
            else:
                await self._claim(drop, interaction)
        except UserError as exc:
            await self._respond_error(interaction, exc)
        except Exception:
            LOGGER.exception("处理发码按钮失败：user=%s", getattr(interaction.user, "id", "?"))
            await self._respond_error(interaction, UserError("errors.unexpected"))

    async def _enter(self, drop: KeyDrop, interaction: discord.Interaction) -> None:
        """抽奖报名。"""
        t = self.bot.t
        reason = await self._ineligible_reason(drop, interaction)
        if reason == "denied":
            raise UserError("keys.drop.role_denied")
        if reason == "need_role":
            raise UserError("keys.drop.need_role", role=self._role_mention(drop, interaction))
        if await self.deliveries.already_claimed(
            drop.guild_id, batch_id=drop.batch_id, discord_user_id=interaction.user.id
        ):
            raise UserError("keys.already_claimed")
        if not await self.drops.enter(drop.drop_id, interaction.user.id):
            raise UserError("keys.drop.already_entered")

        count = await self.drops.entry_count(drop.drop_id)
        await interaction.response.send_message(t(interaction, "keys.drop.entered", count=count), ephemeral=True)

    async def _claim(self, drop: KeyDrop, interaction: discord.Interaction) -> None:
        """先到先得：抢名额 → 取码 → 私信。"""
        t = self.bot.t
        reason = await self._ineligible_reason(drop, interaction)
        if reason == "denied":
            raise UserError("keys.drop.role_denied")
        if reason == "need_role":
            raise UserError("keys.drop.need_role", role=self._role_mention(drop, interaction))
        if await self.deliveries.already_claimed(
            drop.guild_id, batch_id=drop.batch_id, discord_user_id=interaction.user.id
        ):
            raise UserError("keys.already_claimed")

        # 名额：一条 SQL 自增封顶，并发点击不会超发
        if not await self.drops.take_slot(drop.drop_id):
            await self._close_if_done(drop)
            raise UserError("keys.drop.none_left")

        await interaction.response.defer(ephemeral=True)
        if await self._deliver_one(drop, interaction.user):
            await interaction.edit_original_response(
                embed=ok_embed(t(interaction, "keys.claimed.title"), t(interaction, "keys.drop.sent"))
            )
            await self._close_if_done(drop)
            return
        await interaction.edit_original_response(
            embed=info_embed(t(interaction, "keys.drop.not_sent_title"), t(interaction, "keys.dm_failed"))
        )

    # ------------------------------------------------------------------ /key close

    @key_group.command(
        name="close",
        description=localized("Close a drop now (a raffle draws immediately)", "commands.key.close.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(drop_id=localized("Drop id", "commands.key.param_drop_id"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def key_close(self, interaction: discord.Interaction, drop_id: str) -> None:
        # 编号是大写字母表生成的，用户小写输入也认。
        drop_id = drop_id.strip().upper()
        username_guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        drop = await self.drops.get(drop_id)
        if drop is None or drop.guild_id != username_guild.id:
            raise UserError("keys.drop.not_found", drop=drop_id)
        if drop.status != OPEN:
            raise UserError("keys.drop.closed_already", drop=drop_id)

        await interaction.response.defer(ephemeral=True)
        if drop.is_raffle:
            await self.draw(drop)
        else:
            await self.drops.set_status(drop.drop_id, CLOSED)
            await self._update_drop_message(drop, closed=True)
        t = self.bot.t
        await interaction.edit_original_response(
            embed=ok_embed(
                t(interaction, "keys.drop.closed_title"),
                t(interaction, "keys.drop.closed_body", drop=drop_id),
            )
        )

    # ------------------------------------------------------------------ 黑名单

    @key_group.command(
        name="deny-role",
        description=localized("Members with this role can never claim keys", "commands.key.deny_role.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(role=localized("Role to exclude", "commands.key.param_role"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def key_deny_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        added = await self.denied.add(guild.id, role.id, created_by=interaction.user.id)
        await self._reply_denied_roles(interaction, guild, role=role, added=added)

    @key_group.command(
        name="allow-role",
        description=localized("Remove a role from the deny list", "commands.key.allow_role.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(role=localized("Role to allow again", "commands.key.param_role"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def key_allow_role(self, interaction: discord.Interaction, role: discord.Role) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        removed = await self.denied.remove(guild.id, role.id)
        await self._reply_denied_roles(interaction, guild, role=role, added=None if removed else False)

    async def _reply_denied_roles(
        self,
        interaction: discord.Interaction,
        guild: discord.Guild,
        *,
        role: discord.Role,
        added: bool | None,
    ) -> None:
        """回执里带上**当前完整名单** —— 这是唯一能看到黑名单的地方，免得有人猜。"""
        t = self.bot.t
        denied = await self.denied.all(guild.id)
        listing = "、".join(f"<@&{role_id}>" for role_id in sorted(denied)) or t(interaction, "keys.deny.none")
        if added is True:
            head = t(interaction, "keys.deny.added", role=role.mention)
        elif added is None:
            head = t(interaction, "keys.deny.removed", role=role.mention)
        else:
            # added is False：要么本来就在名单里（添加时），要么本来就不在（移除时）
            head = t(interaction, "keys.deny.unchanged", role=role.mention)
        await interaction.response.send_message(
            embed=info_embed(
                t(interaction, "keys.deny.title"), f"{head}\n\n{t(interaction, 'keys.deny.current', roles=listing)}"
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ /key list

    @key_group.command(
        name="list",
        description=localized("Who received a key from this batch", "commands.key.list.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(batch=localized("Batch id", "commands.key.param_batch"))
    @app_commands.checks.has_permissions(manage_guild=True)
    async def key_list(self, interaction: discord.Interaction, batch: str) -> None:
        guild = self._require_guild(interaction)
        await self._require_linked(interaction)
        batch_id = batch.strip()
        rows = await self.deliveries.for_batch(guild.id, batch_id)
        t = self.bot.t
        title = t(interaction, "keys.list.title", batch=batch_id)
        if not rows:
            await interaction.response.send_message(
                embed=info_embed(title, t(interaction, "keys.list.empty")), ephemeral=True
            )
            return
        lines = [
            t(
                interaction,
                "keys.list.entry",
                member=f"<@{row.discord_user_id}>",
                prefix=row.key_prefix,
                time=row.delivered_at,
            )
            for row in rows
        ]
        await interaction.response.send_message(
            embed=info_embed(title, t(interaction, "keys.list.total", count=len(rows)) + "\n" + "\n".join(lines)),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 发码与消息维护

    async def _deliver_one(self, drop: KeyDrop, user: discord.abc.User) -> bool:
        """给一个人发一枚码。任何一步失败都会把码/名额还原。"""
        client = self._require_client()
        try:
            keys = await take_keys(client, team_id=drop.team_id, batch_id=drop.batch_id, count=1, recipient=user.id)
        except KeyPoolError as exc:
            LOGGER.warning("取码失败：batch=%s code=%s", drop.batch_id, exc.code)
            await self.drops.give_back_slot(drop.drop_id)
            return False
        if not keys:
            await self.drops.give_back_slot(drop.drop_id)
            return False

        key = keys[0]
        if not await self.deliveries.claim(
            drop.guild_id,
            batch_id=drop.batch_id,
            discord_user_id=user.id,
            key_id=key.key_id,
            key_prefix=key.key_prefix,
        ):
            await release_key(client, team_id=drop.team_id, key_id=key.key_id)
            await self.drops.give_back_slot(drop.drop_id)
            return False

        try:
            locale = await self._locale(drop.guild_id)
            await user.send(self.bot.i18n.t(locale, "keys.dm", batch=drop.batch_id, code=key.plaintext))
        except discord.HTTPException:
            await release_key(client, team_id=drop.team_id, key_id=key.key_id)
            await self.deliveries.forget(drop.guild_id, batch_id=drop.batch_id, discord_user_id=user.id)
            await self.drops.give_back_slot(drop.drop_id)
            LOGGER.warning("私信失败，已退码：drop=%s key=%s", drop.drop_id, key.key_id)
            return False

        LOGGER.info("已发放激活码：drop=%s key=%s to=%s", drop.drop_id, key.key_id, user.id)
        return True

    async def _resolve_user(self, user_id: int) -> discord.abc.User | None:
        user = self.bot.get_user(user_id)
        if user is not None:
            return user
        try:
            return await self.bot.fetch_user(user_id)
        except discord.HTTPException:
            return None

    async def _close_if_done(self, drop: KeyDrop) -> None:
        latest = await self.drops.get(drop.drop_id)
        if latest is not None and latest.delivered >= latest.key_count and latest.status == OPEN:
            await self.drops.set_status(drop.drop_id, CLOSED)
            await self._update_drop_message(latest, closed=True)

    async def _update_drop_message(
        self, drop: KeyDrop, *, closed: bool, extra_key: str | None = None, **extra: Any
    ) -> None:
        if drop.message_id is None:
            return
        channel = self.bot.get_channel(drop.channel_id)
        if channel is None:
            return
        try:
            message = await channel.fetch_message(drop.message_id)
        except discord.HTTPException:
            LOGGER.warning("找不到发码消息：drop=%s", drop.drop_id)
            return
        embed = message.embeds[0] if message.embeds else None
        if embed is not None and extra_key:
            locale = await self._locale(drop.guild_id)
            embed.add_field(name="\u200b", value=self.bot.i18n.t(locale, extra_key, **extra), inline=False)
        try:
            await message.edit(embed=embed, view=None if closed else AccessKeysView(self))
        except discord.HTTPException:
            LOGGER.warning("更新发码消息失败：drop=%s", drop.drop_id)

    async def _respond_error(self, interaction: discord.Interaction, error: UserError) -> None:
        text = self.bot.i18n.t(interaction.locale, error.key, **error.kwargs)
        try:
            if interaction.response.is_done():
                await interaction.followup.send(text, ephemeral=True)
            else:
                await interaction.response.send_message(text, ephemeral=True)
        except discord.HTTPException:
            LOGGER.exception("回传按钮错误提示失败")

    # ------------------------------------------------------------------ 小工具

    async def _ineligible_reason(self, drop: KeyDrop, interaction: discord.Interaction) -> str | None:
        """不能参与的原因；能参与返回 ``None``。

        **黑名单先于白名单**：被排除的角色压过活动上的资格设置（先排除，再看资格）。
        """
        role_ids = [getattr(item, "id", 0) for item in (getattr(interaction.user, "roles", None) or [])]
        if await self.denied.blocks(drop.guild_id, role_ids):
            return "denied"
        if drop.role_id is not None and drop.role_id not in role_ids:
            return "need_role"
        return None

    def _role_mention(self, drop: KeyDrop, interaction: discord.Interaction) -> str:
        guild = self._require_guild(interaction)
        role = guild.get_role(drop.role_id) if drop.role_id else None
        return role.mention if role is not None else f"<@&{drop.role_id}>"

    def _require_client(self) -> Any:
        if self._client is None:
            raise UserError("smas.not_configured")
        return self._client

    @staticmethod
    def _require_guild(interaction: discord.Interaction) -> discord.Guild:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        return interaction.guild
