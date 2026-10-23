"""`/key` 面板（阶段一）：本地 key 池的导入、列表与详情。

活动创建与领取在下一阶段接入；这里先把池的管理路径做通，避免半成品按钮。
"""

from __future__ import annotations

import asyncio
import csv
import io
import logging
import random
from datetime import timedelta
from typing import Any

import discord
from discord import app_commands
from discord.ext import commands

from bot.core.clock import parse_iso, utcnow
from bot.core.errors import UserError
from bot.core.translator import localized
from bot.core.ui import error_embed, info_embed, ok_embed, send_paged_lines, warn_embed
from bot.integrations.keypool import (
    CARD_ANNOUNCEMENT,
    CARD_CLOSED,
    COUNT_FIXED,
    COUNT_FOLLOW,
    DROP_CLOSED,
    DROP_OPEN,
    DROP_SETTLING,
    MODE_FCFS,
    MODE_RAFFLE,
    DropError,
    KeyDrop,
    KeyPoolError,
    PoolInUse,
    PoolNameTaken,
    PoolNotEmpty,
    Reservation,
)
from bot.modules.access_keys.importing import KeyFileError, ParsedKeys, parse_key_file
from bot.modules.access_keys.pool_service import KeyPoolService

LOGGER = logging.getLogger("bot.access_keys")
SWEEP_SECONDS = 30


class AccessKeysCog(commands.Cog):
    def __init__(self, bot: Any, service: KeyPoolService) -> None:
        self.bot = bot
        self.service = service
        self.store = service.store
        self.log = LOGGER
        self._sweeper: asyncio.Task[None] | None = None
        self._lock = asyncio.Lock()
        bot.services.unregister("keys")
        bot.services.register("keys", service)
        bot.add_view(RaffleCardView(self))
        bot.add_view(FcfsCardView(self))
        bot.add_view(RetryView(self))

    async def cog_unload(self) -> None:
        self.bot.services.unregister("keys")
        self.stop_sweeper()

    def t(self, interaction: discord.Interaction, key: str, **kwargs: Any) -> str:
        return self.bot.t(interaction, key, **kwargs)

    def t_locale(self, locale: Any, key: str, **kwargs: Any) -> str:
        return self.bot.i18n.t(locale, key, **kwargs)

    # ------------------------------------------------------------------ 命令

    @app_commands.command(
        name="key",
        description=localized("Manage the local key pool", "commands.key.description"),
        extras={"permissions": ("manage_guild",)},
    )
    @app_commands.describe(file=localized("Import a .txt or .csv key file", "commands.key.param_file"))
    @app_commands.default_permissions(manage_guild=True)
    @app_commands.checks.has_permissions(manage_guild=True)
    async def key(
        self,
        interaction: discord.Interaction,
        file: discord.Attachment | None = None,
    ) -> None:
        if interaction.guild is None:
            raise UserError("errors.guild_only")
        if file is not None:
            await self._import_command(interaction, file)
            return
        await self._show_main(interaction, edit=False)

    # ------------------------------------------------------------------ 主面板

    async def _show_main(self, interaction: discord.Interaction, *, edit: bool) -> None:
        summaries = await self.service.summaries(interaction.guild_id)
        available = sum(item.stats.available for item in summaries)
        embed = info_embed(
            self.t(interaction, "keys.panel.title"),
            self.t(interaction, "keys.panel.description", pools=len(summaries), available=available),
        )
        view = MainView(self, interaction.user.id, interaction.locale)
        await _respond(interaction, embed=embed, view=view, edit=edit)

    # ------------------------------------------------------------------ 导入

    async def _import_command(self, interaction: discord.Interaction, file: discord.Attachment) -> None:
        try:
            parsed = parse_key_file(file.filename, await file.read())
        except KeyFileError as exc:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.import.failed"), str(exc)),
                ephemeral=True,
            )
            return
        if not parsed.keys:
            await interaction.response.send_message(
                embed=error_embed(
                    self.t(interaction, "keys.import.no_keys"),
                    self._invalid_hint(interaction, parsed),
                ),
                ephemeral=True,
            )
            return

        summaries = await self.service.summaries(interaction.guild_id)
        if not summaries:
            await interaction.response.send_modal(NewPoolImportModal(self, parsed))
            return
        await interaction.response.send_message(
            embed=self._import_embed(interaction, parsed),
            view=ImportPoolView(self, interaction.user.id, interaction.locale, parsed, summaries),
            ephemeral=True,
        )

    def _import_embed(self, interaction: discord.Interaction, parsed: ParsedKeys) -> discord.Embed:
        return warn_embed(
            self.t(interaction, "keys.import.title"),
            self.t(
                interaction,
                "keys.import.summary",
                valid=len(parsed.keys),
                duplicates=parsed.duplicates_in_file,
                invalid=len(parsed.invalid),
            )
            + (
                "\n" + self._invalid_hint(interaction, parsed)
                if parsed.invalid
                else ""
            ),
        )

    def _invalid_hint(self, interaction: discord.Interaction, parsed: ParsedKeys) -> str:
        if not parsed.invalid:
            return ""
        lines = [self.t(interaction, "keys.import.invalid_lines")]
        lines.extend(
            self.t(interaction, "keys.import.invalid_line", line=line, reason=reason)
            for line, reason in parsed.invalid[:10]
        )
        return "\n".join(lines)

    async def _import_into(self, interaction: discord.Interaction, parsed: ParsedKeys, pool_id: int) -> None:
        inserted, skipped = await self.store.add_keys(interaction.guild_id, pool_id, parsed.keys)
        pool = await self.store.get_pool(interaction.guild_id, pool_id)
        await interaction.response.send_message(
            embed=ok_embed(
                self.t(interaction, "keys.import.done"),
                self.t(
                    interaction,
                    "keys.import.done_body",
                    pool=pool.name if pool is not None else str(pool_id),
                    inserted=inserted,
                    skipped=skipped,
                ),
            ),
            ephemeral=True,
        )

    # ------------------------------------------------------------------ 池

    async def _show_pools(self, interaction: discord.Interaction) -> None:
        summaries = await self.service.summaries(interaction.guild_id)
        if not summaries:
            await interaction.response.edit_message(
                embed=info_embed(self.t(interaction, "keys.pool.title"), self.t(interaction, "keys.pool.none")),
                view=BackView(self, interaction.user.id, interaction.locale),
            )
            return
        await interaction.response.edit_message(
            embed=info_embed(self.t(interaction, "keys.pool.title"), self.t(interaction, "keys.pool.pick")),
            view=PoolListView(self, interaction.user.id, interaction.locale, summaries),
        )

    async def _show_pool_detail(self, interaction: discord.Interaction, pool_id: int) -> None:
        pool = await self.store.get_pool(interaction.guild_id, pool_id)
        if pool is None:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.pool.missing")), ephemeral=True
            )
            return
        stats = await self.store.pool_stats(interaction.guild_id, pool_id)
        await interaction.response.edit_message(
            embed=info_embed(
                self.t(interaction, "keys.pool.detail_title", pool=pool.name),
                self.t(
                    interaction,
                    "keys.pool.detail_body",
                    available=stats.available,
                    reserved=stats.reserved,
                    delivered=stats.delivered,
                    void=stats.void,
                    total=stats.total,
                    state=self.t(
                        interaction, "keys.pool.enabled" if pool.enabled else "keys.pool.disabled"
                    ),
                ),
            ),
            view=PoolDetailView(self, interaction.user.id, interaction.locale, pool.id, pool.enabled),
        )

    async def _export_pool(self, interaction: discord.Interaction, pool_id: int) -> None:
        pool = await self.store.get_pool(interaction.guild_id, pool_id)
        if pool is None:
            raise UserError("keys.pool.missing")
        rows = await self.store.list_keys(interaction.guild_id, pool_id)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(
            ["pool", "key", "status", "added_at", "delivered_at", "delivered_to_id", "drop_id", "voided_at"]
        )
        for row in rows:
            writer.writerow(
                [
                    row.pool_name,
                    row.secret,
                    row.status,
                    row.added_at,
                    row.delivered_at or "",
                    row.delivered_to or "",
                    row.drop_id or "",
                    row.voided_at or "",
                ]
            )
        data = buffer.getvalue().encode("utf-8-sig")
        await interaction.response.send_message(
            file=discord.File(io.BytesIO(data), filename=f"pool-{pool_id}.csv"),
            ephemeral=True,
        )


    # ------------------------------------------------------------------ 活动

    async def _start_drop(self, interaction: discord.Interaction) -> None:
        summaries = [
            item
            for item in await self.service.summaries(interaction.guild_id)
            if item.pool.enabled and item.stats.available > 0
        ]
        if not summaries:
            await interaction.response.edit_message(
                embed=info_embed(
                    self.t(interaction, "keys.drop.title"),
                    self.t(interaction, "keys.drop.no_stock"),
                ),
                view=BackView(self, interaction.user.id, interaction.locale),
            )
            return
        await interaction.response.edit_message(
            embed=info_embed(
                self.t(interaction, "keys.drop.pick_pools"),
                self.t(interaction, "keys.drop.pick_hint"),
            ),
            view=DropPoolView(self, interaction.user.id, interaction.locale, summaries),
        )

    async def _show_drop_setup(self, interaction: discord.Interaction, pool_ids: list[int]) -> None:
        available = await self.service.available(interaction.guild_id, pool_ids)
        roles = [role for role in interaction.guild.roles if not role.is_default()]
        roles.sort(key=lambda role: role.position, reverse=True)
        view = DropSetupView(
            self,
            interaction.user.id,
            interaction.locale,
            pool_ids=pool_ids,
            available=available,
            roles=roles,
        )
        await interaction.response.edit_message(
            embed=self._drop_setup_embed(interaction, view),
            view=view,
        )

    def _drop_setup_embed(self, interaction: discord.Interaction, view: DropSetupView) -> discord.Embed:
        lines = [
            self.t(interaction, "keys.drop.setup_pools", count=len(view.pool_ids), available=view.available),
            self.t(
                interaction,
                "keys.drop.setup_mode",
                mode=self.t(
                    interaction,
                    "keys.mode.raffle" if view.mode == MODE_RAFFLE else "keys.mode.fcfs",
                ),
            ),
            self.t(
                interaction,
                "keys.drop.setup_count",
                count=(
                    self.t(interaction, "keys.count.follow")
                    if view.count_mode == COUNT_FOLLOW
                    else self.t(interaction, "keys.count.fixed", count=view.key_count or 0)
                ),
            ),
        ]
        lines.append(
            self.t(
                interaction,
                "keys.drop.setup_role",
                role=f"<@&{view.role_id}>" if view.role_id else self.t(interaction, "keys.drop.role_none"),
            )
        )
        lines.append(
            self.t(
                interaction,
                "keys.drop.setup_deny",
                role=f"<@&{view.deny_role_id}>"
                if view.deny_role_id
                else self.t(interaction, "keys.drop.role_none"),
            )
        )
        if view.mode == MODE_RAFFLE:
            lines.append(self.t(interaction, "keys.drop.setup_minutes", minutes=view.minutes))
        lines.append(
            self.t(
                interaction,
                "keys.drop.setup_entry",
                state=self.t(
                    interaction,
                    "keys.drop.entry_on" if view.auto_entry else "keys.drop.entry_off",
                ),
            )
        )
        return info_embed(self.t(interaction, "keys.drop.setup_title"), "\n".join(lines))

    async def _create_drop(
        self,
        interaction: discord.Interaction,
        *,
        pool_ids: list[int],
        mode: str,
        count_mode: str,
        key_count: int | None,
        role_id: int | None,
        deny_role_id: int | None,
        minutes: int,
        auto_entry: bool = True,
    ) -> None:
        if role_id is not None and role_id == deny_role_id:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.drop.same_role")), ephemeral=True
            )
            return
        closes_at = None
        if mode == MODE_RAFFLE:
            closes_at = (utcnow() + timedelta(minutes=minutes)).isoformat(timespec="seconds")
        available = await self.service.available(interaction.guild_id, pool_ids)
        warning = ""
        if count_mode == COUNT_FIXED and key_count is not None and key_count > available:
            warning = self.t(
                interaction, "keys.drop.low_stock", count=key_count, available=available
            )
        try:
            drop = await self.store.create_drop(
                interaction.guild_id,
                pool_ids=pool_ids,
                mode=mode,
                count_mode=count_mode,
                key_count=key_count if count_mode == COUNT_FIXED else None,
                role_id=role_id,
                deny_role_id=deny_role_id,
                channel_id=interaction.channel_id or 0,
                closes_at=closes_at,
                created_by=interaction.user.id,
            )
        except (DropError, KeyPoolError) as exc:
            await interaction.response.send_message(embed=error_embed(str(exc)), ephemeral=True)
            return

        channel = interaction.channel
        if auto_entry and not isinstance(channel, discord.abc.Messageable):
            await self.store.close_drop(interaction.guild_id, drop.id)
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.drop.bad_channel")), ephemeral=True
            )
            return

        if not auto_entry:
            await interaction.response.send_message(
                embed=ok_embed(
                    self.t(interaction, "keys.drop.created"),
                    self.t(interaction, "keys.drop.created_no_entry", code=drop.code),
                ),
                ephemeral=True,
            )
            return

        assert isinstance(channel, discord.abc.Messageable)
        view: discord.ui.View = RaffleCardView(self) if mode == MODE_RAFFLE else FcfsCardView(self)
        try:
            message = await channel.send(embed=self._card_embed(interaction, drop, len(pool_ids)), view=view)
        except discord.HTTPException:
            await self.store.close_drop(interaction.guild_id, drop.id)
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.drop.send_failed")), ephemeral=True
            )
            return

        await self.store.add_card(
            drop.id,
            channel_id=channel.id,
            message_id=message.id,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=ok_embed(
                self.t(interaction, "keys.drop.created"),
                self.t(interaction, "keys.drop.created_body", code=drop.code, channel=channel.mention)
                + (f"\n\n{warning}" if warning else ""),
            ),
            ephemeral=True,
        )

    async def _add_entry(self, interaction: discord.Interaction, drop: KeyDrop) -> None:
        current = await self.store.get_drop_by_id(interaction.guild_id, drop.id)
        if current is None or current.status == DROP_CLOSED:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.drop.closed_already")), ephemeral=True
            )
            return
        channel = interaction.channel
        if not isinstance(channel, discord.abc.Messageable):
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.drop.bad_channel")), ephemeral=True
            )
            return
        old_cards = await self.store.deactivate_cards_in_channel(current.id, channel.id)
        for card in old_cards:
            if card.message_id is None:
                continue
            try:
                message = await channel.fetch_message(card.message_id)
                await message.edit(view=None)
            except discord.HTTPException:
                self.log.debug("撤销旧入口失败：%s", card.id)
        view: discord.ui.View = RaffleCardView(self) if current.mode == MODE_RAFFLE else FcfsCardView(self)
        try:
            message = await channel.send(
                embed=self._card_embed(interaction, current, len(await self.store.drop_pools(current.id))),
                view=view,
            )
        except discord.HTTPException:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.drop.send_failed")), ephemeral=True
            )
            return
        await self.store.add_card(
            current.id,
            channel_id=channel.id,
            message_id=message.id,
            created_by=interaction.user.id,
        )
        await interaction.response.send_message(
            embed=ok_embed(
                self.t(interaction, "keys.drop.entry_added"),
                self.t(interaction, "keys.drop.entry_added_body", code=current.code, channel=channel.mention),
            ),
            ephemeral=True,
        )

    def _card_embed(self, interaction: discord.Interaction, drop: KeyDrop, pool_count: int) -> discord.Embed:
        if drop.mode == MODE_RAFFLE:
            title = self.t(interaction, "keys.card.raffle_title")
            body = self.t(
                interaction,
                "keys.card.raffle_body",
                pools=pool_count,
                count=self._count_label(interaction, drop),
                deadline=self._deadline(interaction, drop),
            )
        else:
            title = self.t(interaction, "keys.card.fcfs_title")
            body = self.t(
                interaction,
                "keys.card.fcfs_body",
                pools=pool_count,
                count=self._count_label(interaction, drop),
            )
        if drop.role_id is not None:
            body += "\n" + self.t(interaction, "keys.card.role", role=f"<@&{drop.role_id}>")
        if drop.deny_role_id is not None:
            body += "\n" + self.t(interaction, "keys.card.deny", role=f"<@&{drop.deny_role_id}>")
        return info_embed(title, body)

    async def _show_active(self, interaction: discord.Interaction) -> None:
        drops = await self.service.active_drops(interaction.guild_id)
        if not drops:
            await interaction.response.edit_message(
                embed=info_embed(self.t(interaction, "keys.active.title"), self.t(interaction, "keys.active.none")),
                view=BackView(self, interaction.user.id, interaction.locale),
            )
            return
        await interaction.response.edit_message(
            embed=info_embed(self.t(interaction, "keys.active.title"), self.t(interaction, "keys.active.pick")),
            view=ActiveListView(self, interaction.user.id, interaction.locale, drops),
        )

    async def _show_active_detail(self, interaction: discord.Interaction, drop: KeyDrop) -> None:
        pools = await self.store.drop_pools(drop.id)
        available = await self.service.available(interaction.guild_id, [pool.id for pool in pools])
        body = self.t(
            interaction,
            "keys.active.detail_body",
            pools=len(pools),
            mode=self._mode_label(interaction, drop.mode),
            count=self._count_label(interaction, drop),
            delivered=drop.delivered,
            available=available,
            status=drop.status,
        )
        if drop.count_mode == COUNT_FIXED and drop.key_count is not None:
            remaining = max(drop.key_count - drop.delivered, 0)
            if available < remaining:
                body += "\n" + self.t(
                    interaction,
                    "keys.drop.low_stock_active",
                    remaining=remaining,
                    available=available,
                )
        await interaction.response.edit_message(
            embed=info_embed(self.t(interaction, "keys.active.detail_title", code=drop.code), body),
            view=ActiveDetailView(self, interaction.user.id, interaction.locale, drop),
        )

    async def _show_add_pool(self, interaction: discord.Interaction, drop: KeyDrop) -> None:
        current = {pool.id for pool in await self.store.drop_pools(drop.id)}
        summaries = [
            item
            for item in await self.service.summaries(interaction.guild_id, include_disabled=False)
            if item.pool.id not in current
        ]
        if not summaries:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.active.no_pools")), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=info_embed(self.t(interaction, "keys.active.add_pool"), self.t(interaction, "keys.active.pick_pool")),
            view=AddPoolView(self, interaction.user.id, interaction.locale, drop, summaries),
        )

    async def _show_remove_pool(self, interaction: discord.Interaction, drop: KeyDrop) -> None:
        pools = await self.store.drop_pools(drop.id)
        if len(pools) <= 1:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.active.cannot_remove_last")), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=info_embed(self.t(interaction, "keys.active.remove_pool"), self.t(interaction, "keys.active.pick_pool")),
            view=RemovePoolView(self, interaction.user.id, interaction.locale, drop, pools),
        )

    async def _show_entries(self, interaction: discord.Interaction, drop: KeyDrop) -> None:
        cards = [card for card in await self.store.cards(drop.id) if card.status == "active"]
        if not cards:
            await interaction.response.send_message(
                embed=error_embed(self.t(interaction, "keys.active.no_entries")), ephemeral=True
            )
            return
        await interaction.response.edit_message(
            embed=info_embed(
                self.t(interaction, "keys.active.manage_entries"),
                self.t(interaction, "keys.active.pick_entry"),
            ),
            view=EntryListView(self, interaction.user.id, interaction.locale, drop, cards),
        )

    async def _show_records(self, interaction: discord.Interaction) -> None:
        drops = await self.store.list_drops(interaction.guild_id, limit=25)
        if not drops:
            await interaction.response.send_message(
                embed=info_embed(self.t(interaction, "keys.records.title"), self.t(interaction, "keys.records.none")),
                ephemeral=True,
            )
            return
        lines = [
            self.t(
                interaction,
                "keys.records.line",
                code=drop.code,
                mode=self._mode_label(interaction, drop.mode),
                delivered=drop.delivered,
                count=self._count_label(interaction, drop),
                status=drop.status,
            )
            for drop in drops
        ]
        await send_paged_lines(
            interaction,
            title=self.t(interaction, "keys.records.title"),
            lines=lines,
            translate=lambda key, **kw: self.t(interaction, key, **kw),
        )

    async def _close_drop(self, interaction: discord.Interaction, drop: KeyDrop) -> None:
        current = await self.store.get_drop_by_id(interaction.guild_id, drop.id)
        if current is not None and current.status == DROP_SETTLING:
            await self.store.close_drop(interaction.guild_id, current.id, status=DROP_CLOSED)
            await self.store.set_retry_until(interaction.guild_id, current.id, None)
            await self._update_cards(current)
        elif current is not None and current.status == DROP_OPEN:
            if current.mode == MODE_RAFFLE:
                await self._draw_drop(interaction.guild, current)
            else:
                await self.store.close_drop(interaction.guild_id, current.id)
                await self._update_cards(current)
        await interaction.response.edit_message(
            embed=ok_embed(
                self.t(interaction, "keys.active.closed"),
                self.t(interaction, "keys.active.closed_body", code=drop.code),
            ),
            view=BackView(self, interaction.user.id, interaction.locale),
        )

    async def _draw_drop(self, guild: discord.Guild, drop: KeyDrop) -> tuple[list[int], list[int]]:
        current = await self.store.get_drop_by_id(guild.id, drop.id)
        if current is None or current.status != DROP_OPEN:
            return [], []
        drop = current
        locale = await self._guild_locale(guild.id)
        pools = await self.store.drop_pools(drop.id)
        pool_ids = [pool.id for pool in pools]
        available = await self.service.available(guild.id, pool_ids)
        entries = await self.store.entries(drop.id)
        target = drop.key_count if drop.count_mode == COUNT_FIXED and drop.key_count is not None else available
        target = min(target, available, len(entries))
        winners = entries[:target]
        random.SystemRandom().shuffle(winners)
        delivered: list[int] = []
        failures: list[int] = []
        for user_id, _ in winners:
            reservation = await self.service.reserve(guild.id, pool_ids, user_id=user_id, drop_id=drop.id)
            if reservation is None:
                failures.append(user_id)
                await self.store.record_failure(drop.id, user_id, "no_stock")
                continue
            if await self._deliver(guild, user_id, reservation, locale):
                delivered.append(user_id)
                await self.store.clear_failure(drop.id, user_id)
            else:
                failures.append(user_id)
                await self.store.record_failure(drop.id, user_id, "dm_failed")

        await self.store.close_drop(
            guild.id, drop.id, status=DROP_SETTLING if failures else DROP_CLOSED
        )
        if failures:
            retry_until = (utcnow() + timedelta(hours=24)).isoformat(timespec="seconds")
            await self.store.set_retry_until(guild.id, drop.id, retry_until)
        else:
            await self.store.set_retry_until(guild.id, drop.id, None)
        await self._update_cards(drop)
        await self._announce(guild, drop, delivered, failures, locale)
        return delivered, failures

    async def _deliver(
        self,
        guild: discord.Guild,
        user_id: int,
        reservation: Reservation,
        locale: str,
    ) -> bool:
        member = guild.get_member(user_id)
        if member is None:
            await self.service.release(reservation)
            return False
        try:
            await member.send(self.bot.i18n.t(locale, "keys.dm.body", code=reservation.secret))
        except discord.HTTPException:
            await self.service.release(reservation)
            return False
        await self.service.commit(reservation)
        return True

    async def _announce(
        self,
        guild: discord.Guild,
        drop: KeyDrop,
        delivered: list[int],
        failures: list[int],
        locale: str,
    ) -> None:
        channel = guild.get_channel(drop.channel_id)
        if not isinstance(channel, discord.abc.Messageable):
            return
        lines: list[str] = []
        if delivered:
            lines.append(
                self.bot.i18n.t(
                    locale,
                    "keys.draw.winners",
                    winners="、".join(f"<@{user_id}>" for user_id in delivered),
                )
            )
        if failures:
            lines.append(
                self.bot.i18n.t(
                    locale,
                    "keys.draw.failures",
                    count=len(failures),
                    users="、".join(f"<@{user_id}>" for user_id in failures),
                )
            )
        if not delivered and not failures:
            lines.append(self.bot.i18n.t(locale, "keys.draw.none"))
        try:
            message = await channel.send(
                embed=info_embed(
                    self.bot.i18n.t(locale, "keys.draw.title", code=drop.code),
                    "\n".join(lines) or None,
                ),
                view=RetryView(self) if failures else None,
            )
        except discord.HTTPException:
            self.log.exception("发送开奖公告失败")
            return
        if failures:
            await self.store.add_card(
                drop.id,
                channel_id=channel.id,
                message_id=message.id,
                created_by=guild.me.id if guild.me is not None else 0,
                status=CARD_ANNOUNCEMENT,
            )

    async def _update_cards(self, drop: KeyDrop) -> None:
        for card in await self.store.cards(drop.id):
            if card.status != "active" or card.message_id is None:
                continue
            await self.store.set_card_status(card.id, CARD_CLOSED)
            guild = self.bot.get_guild(drop.guild_id)
            channel = guild.get_channel(card.channel_id) if guild is not None else None
            if not isinstance(channel, discord.abc.Messageable):
                continue
            try:
                message = await channel.fetch_message(card.message_id)
                await message.edit(view=None)
            except discord.HTTPException:
                self.log.debug("更新入口卡片失败：%s", card.id)

    # ------------------------------------------------------------------ 领取

    async def _handle_raffle(self, interaction: discord.Interaction) -> None:
        try:
            drop = await self.store.drop_by_card(interaction.message.id)
            if drop is None or drop.status != DROP_OPEN:
                raise UserError("keys.drop.closed_already")
            denial = self._eligibility(interaction, drop)
            if denial:
                raise UserError(denial)
            if not await self.store.add_entry(drop.id, interaction.user.id):
                raise UserError("keys.drop.already_entered")
            count = await self.store.entry_count(drop.id)
            await interaction.response.send_message(
                embed=ok_embed(
                    self.t(interaction, "keys.enter.title"),
                    self.t(interaction, "keys.enter.body", count=count),
                ),
                ephemeral=True,
            )
        except UserError as exc:
            await self._reply_error(interaction, exc)

    async def _handle_fcfs(self, interaction: discord.Interaction) -> None:
        try:
            drop = await self.store.drop_by_card(interaction.message.id)
            if drop is None or drop.status != DROP_OPEN:
                raise UserError("keys.drop.closed_already")
            denial = self._eligibility(interaction, drop)
            if denial:
                raise UserError(denial)
            if await self.store.has_delivery(drop.id, interaction.user.id):
                raise UserError("keys.claim.already")
            if (
                drop.count_mode == COUNT_FIXED
                and drop.key_count is not None
                and drop.delivered >= drop.key_count
            ):
                await self.store.close_drop(interaction.guild_id, drop.id)
                await self._update_cards(drop)
                raise UserError("keys.drop.closed_already")
            pools = await self.store.drop_pools(drop.id)
            reservation = await self.service.reserve(
                interaction.guild_id,
                [pool.id for pool in pools],
                user_id=interaction.user.id,
                drop_id=drop.id,
            )
            if reservation is None:
                raise UserError("keys.claim.empty")
            try:
                await interaction.user.send(
                    self.t(interaction, "keys.dm.body", code=reservation.secret)
                )
            except discord.HTTPException:
                await self.service.release(reservation)
                raise UserError("keys.claim.dm_failed") from None
            await self.service.commit(reservation)
            updated = await self.store.get_drop_by_id(interaction.guild_id, drop.id)
            if (
                updated is not None
                and updated.count_mode == COUNT_FIXED
                and updated.key_count is not None
                and updated.delivered >= updated.key_count
            ):
                await self.store.close_drop(interaction.guild_id, updated.id)
                await self._update_cards(updated)
            await interaction.response.send_message(
                embed=ok_embed(self.t(interaction, "keys.claim.title"), self.t(interaction, "keys.claim.body")),
                ephemeral=True,
            )
        except UserError as exc:
            await self._reply_error(interaction, exc)

    async def _handle_retry(self, interaction: discord.Interaction) -> None:
        try:
            drop = await self.store.drop_by_card(interaction.message.id)
            if drop is None:
                raise UserError("keys.drop.closed_already")
            failures = {user_id for user_id, _reason, _at in await self.store.failures(drop.id)}
            if interaction.user.id not in failures:
                raise UserError("keys.retry.not_you")
            if drop.status == DROP_CLOSED:
                raise UserError("keys.retry.expired")
            if drop.retry_until is not None and drop.retry_until <= utcnow().isoformat(timespec="seconds"):
                raise UserError("keys.retry.expired")
            pools = await self.store.drop_pools(drop.id)
            reservation = await self.service.reserve(
                interaction.guild_id,
                [pool.id for pool in pools],
                user_id=interaction.user.id,
                drop_id=drop.id,
            )
            if reservation is None:
                raise UserError("keys.claim.empty")
            try:
                await interaction.user.send(
                    self.t(interaction, "keys.dm.body", code=reservation.secret)
                )
            except discord.HTTPException:
                await self.service.release(reservation)
                raise UserError("keys.claim.dm_failed") from None
            await self.service.commit(reservation)
            await self.store.clear_failure(drop.id, interaction.user.id)
            await interaction.response.send_message(
                embed=ok_embed(self.t(interaction, "keys.retry.title"), self.t(interaction, "keys.retry.body")),
                ephemeral=True,
            )
        except UserError as exc:
            await self._reply_error(interaction, exc)

    async def _reply_error(self, interaction: discord.Interaction, exc: UserError) -> None:
        embed = error_embed(self.t(interaction, exc.key, **exc.kwargs))
        if interaction.response.is_done():
            await interaction.followup.send(embed=embed, ephemeral=True)
        else:
            await interaction.response.send_message(embed=embed, ephemeral=True)

    def _eligibility(self, interaction: discord.Interaction, drop: KeyDrop) -> str | None:
        if interaction.guild is None:
            return "errors.guild_only"
        member = interaction.user
        role_ids = {role.id for role in getattr(member, "roles", ())}
        if drop.role_id is not None and drop.role_id not in role_ids:
            return "keys.drop.need_role"
        if drop.deny_role_id is not None and drop.deny_role_id in role_ids:
            return "keys.drop.role_excluded"
        return None

    # ------------------------------------------------------------------ 辅助

    def _count_label(self, interaction: discord.Interaction, drop: KeyDrop) -> str:
        if drop.count_mode == COUNT_FOLLOW:
            return self.t(interaction, "keys.count.follow")
        return self.t(interaction, "keys.count.fixed", count=drop.key_count or 0)

    def _mode_label(self, interaction: discord.Interaction, mode: str) -> str:
        return self.t(interaction, "keys.mode.raffle" if mode == MODE_RAFFLE else "keys.mode.fcfs")

    def _deadline(self, interaction: discord.Interaction, drop: KeyDrop) -> str:
        parsed = parse_iso(drop.closes_at)
        if parsed is None:
            return self.t(interaction, "keys.deadline.none")
        return discord.utils.format_dt(parsed, "R")

    async def _guild_locale(self, guild_id: int) -> str:
        settings = await self.bot.guild_settings.get(guild_id)
        return settings.locale or self.bot.settings.default_locale

    # ------------------------------------------------------------------ 后台

    def start_sweeper(self) -> None:
        if self._sweeper is None or self._sweeper.done():
            self._sweeper = asyncio.create_task(self._sweep_loop())

    def stop_sweeper(self) -> None:
        if self._sweeper is not None and not self._sweeper.done():
            self._sweeper.cancel()
        self._sweeper = None

    async def _sweep_loop(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_SECONDS)
            try:
                async with self._lock:
                    now = utcnow().isoformat(timespec="seconds")
                    for drop in await self.store.due_drops(now):
                        guild = self.bot.get_guild(drop.guild_id)
                        if guild is not None:
                            await self._draw_drop(guild, drop)
                    for expired in await self.store.due_settling(now):
                        await self.store.close_drop(expired.guild_id, expired.id)
                        await self.store.set_retry_until(expired.guild_id, expired.id, None)
                        await self._update_cards(expired)
            except asyncio.CancelledError:
                raise
            except Exception:
                self.log.exception("key 活动后台扫描失败")


async def _respond(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: discord.ui.View | None,
    edit: bool,
) -> None:
    if edit:
        await interaction.response.edit_message(embed=embed, view=view)
    else:
        await interaction.response.send_message(embed=embed, view=view, ephemeral=True)


class PanelView(discord.ui.View):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, *, timeout: float = 300) -> None:
        super().__init__(timeout=timeout)
        self.cog = cog
        self.author_id = author_id
        self.locale = locale

    def label(self, key: str, **kwargs: Any) -> str:
        return self.cog.t_locale(self.locale, key, **kwargs)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(
                self.cog.t(interaction, "keys.not_yours"), ephemeral=True
            )
            return False
        return True


class MainView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any) -> None:
        super().__init__(cog, author_id, locale)
        import_button = discord.ui.Button(
            label=self.label("keys.button.import"), style=discord.ButtonStyle.primary
        )
        pools_button = discord.ui.Button(
            label=self.label("keys.button.pools"), style=discord.ButtonStyle.secondary
        )
        drop_button = discord.ui.Button(
            label=self.label("keys.button.new_drop"), style=discord.ButtonStyle.success
        )
        active_button = discord.ui.Button(
            label=self.label("keys.button.active"), style=discord.ButtonStyle.secondary
        )
        records_button = discord.ui.Button(
            label=self.label("keys.button.records"), style=discord.ButtonStyle.secondary
        )
        close_button = discord.ui.Button(
            label=self.label("keys.button.close"), style=discord.ButtonStyle.secondary
        )
        import_button.callback = self._import
        pools_button.callback = self._pools
        drop_button.callback = self._new_drop
        active_button.callback = self._active
        records_button.callback = self._records
        close_button.callback = self._close
        for button in (
            import_button,
            pools_button,
            drop_button,
            active_button,
            records_button,
            close_button,
        ):
            self.add_item(button)

    async def _import(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            self.cog.t(interaction, "keys.import.instructions"), ephemeral=True
        )

    async def _pools(self, interaction: discord.Interaction) -> None:
        await self.cog._show_pools(interaction)

    async def _new_drop(self, interaction: discord.Interaction) -> None:
        await self.cog._start_drop(interaction)

    async def _active(self, interaction: discord.Interaction) -> None:
        await self.cog._show_active(interaction)

    async def _records(self, interaction: discord.Interaction) -> None:
        await self.cog._show_records(interaction)

    async def _close(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)


class BackView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any) -> None:
        super().__init__(cog, author_id, locale)
        button = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        button.callback = self._back
        self.add_item(button)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_main(interaction, edit=True)


class PoolListView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, summaries) -> None:
        super().__init__(cog, author_id, locale)
        options = [
            discord.SelectOption(
                label=item.pool.name[:100],
                value=str(item.pool.id),
                description=f"{item.stats.available}/{item.stats.total}",
            )
            for item in summaries[:25]
        ]
        select = discord.ui.Select(placeholder=self.label("keys.pool.select"), options=options)
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if values:
            await self.cog._show_pool_detail(interaction, int(values[0]))

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_main(interaction, edit=True)


class PoolDetailView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, pool_id: int, enabled: bool) -> None:
        super().__init__(cog, author_id, locale)
        self.pool_id = pool_id
        self.enabled = enabled
        specs = [
            ("keys.pool.import_more", discord.ButtonStyle.primary, self._import_more),
            ("keys.pool.disable" if enabled else "keys.pool.enable", discord.ButtonStyle.secondary, self._toggle),
            ("keys.pool.export", discord.ButtonStyle.secondary, self._export),
            ("keys.pool.void", discord.ButtonStyle.danger, self._void),
            ("keys.pool.delete", discord.ButtonStyle.danger, self._delete),
            ("keys.pool.rename", discord.ButtonStyle.secondary, self._rename),
            ("keys.button.back", discord.ButtonStyle.secondary, self._back),
        ]
        for index, (key, style, callback) in enumerate(specs):
            button = discord.ui.Button(label=self.label(key), style=style, row=index // 3)
            button.callback = callback
            self.add_item(button)

    async def _import_more(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(
            self.cog.t(interaction, "keys.import.instructions"), ephemeral=True
        )

    async def _toggle(self, interaction: discord.Interaction) -> None:
        if self.enabled:
            try:
                await self.cog.store.stop_pool(interaction.guild_id, self.pool_id)
            except PoolInUse as exc:
                await interaction.response.send_message(
                    embed=error_embed(
                        self.cog.t(interaction, "keys.pool.in_use", codes=", ".join(exc.codes))
                    ),
                    ephemeral=True,
                )
                return
        else:
            await self.cog.store.set_pool_enabled(interaction.guild_id, self.pool_id, True)
        await self.cog._show_pool_detail(interaction, self.pool_id)

    async def _export(self, interaction: discord.Interaction) -> None:
        await self.cog._export_pool(interaction, self.pool_id)

    async def _rename(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(RenamePoolModal(self.cog, self.pool_id))

    async def _void(self, interaction: discord.Interaction) -> None:
        await self._confirm(
            interaction,
            prompt=self.cog.t(interaction, "keys.pool.void_confirm"),
            action=self._do_void,
        )

    async def _do_void(self, interaction: discord.Interaction) -> None:
        await self.cog.store.void_available(interaction.guild_id, self.pool_id)
        await self.cog._show_pool_detail(interaction, self.pool_id)

    async def _delete(self, interaction: discord.Interaction) -> None:
        await self._confirm(
            interaction,
            prompt=self.cog.t(interaction, "keys.pool.delete_confirm"),
            action=self._do_delete,
        )

    async def _do_delete(self, interaction: discord.Interaction) -> None:
        try:
            await self.cog.store.delete_pool(interaction.guild_id, self.pool_id)
        except (PoolNotEmpty, PoolInUse) as exc:
            await interaction.response.send_message(embed=error_embed(str(exc)), ephemeral=True)
            return
        await self.cog._show_pools(interaction)

    async def _confirm(self, interaction: discord.Interaction, *, prompt: str, action) -> None:
        view = ConfirmActionView(self.cog, interaction.user.id, interaction.locale, prompt=prompt, action=action)
        await interaction.response.send_message(embed=warn_embed(prompt), view=view, ephemeral=True)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_pools(interaction)


class ConfirmActionView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, *, prompt: str, action) -> None:
        super().__init__(cog, author_id, locale)
        self.prompt = prompt
        self.action = action
        confirm = discord.ui.Button(label=self.label("keys.confirm"), style=discord.ButtonStyle.danger)
        cancel = discord.ui.Button(label=self.label("keys.cancel"), style=discord.ButtonStyle.secondary)
        confirm.callback = self._confirm
        cancel.callback = self._cancel
        self.add_item(confirm)
        self.add_item(cancel)

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.action(interaction)

    async def _cancel(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(view=None)


class ImportPoolView(PanelView):
    def __init__(
        self,
        cog: AccessKeysCog,
        author_id: int,
        locale: Any,
        parsed: ParsedKeys,
        summaries,
    ) -> None:
        super().__init__(cog, author_id, locale)
        self.parsed = parsed
        self.pool_id: int | None = None
        options = [
            discord.SelectOption(
                label=item.pool.name[:100],
                value=str(item.pool.id),
                description=f"{item.stats.available}/{item.stats.total}",
            )
            for item in summaries[:25]
        ]
        select = discord.ui.Select(placeholder=self.label("keys.import.select_pool"), options=options)
        select.callback = self._picked
        self.add_item(select)
        confirm = discord.ui.Button(label=self.label("keys.import.confirm"), style=discord.ButtonStyle.success)
        confirm.callback = self._confirm
        self.add_item(confirm)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if values:
            self.pool_id = int(values[0])
        await interaction.response.defer()

    async def _confirm(self, interaction: discord.Interaction) -> None:
        if self.pool_id is None:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "keys.import.pick_pool")), ephemeral=True
            )
            return
        await self.cog._import_into(interaction, self.parsed, self.pool_id)


class RenamePoolModal(discord.ui.Modal):
    def __init__(self, cog: AccessKeysCog, pool_id: int) -> None:
        super().__init__(title="Rename pool")
        self.cog = cog
        self.pool_id = pool_id
        self.name = discord.ui.TextInput(label="New name", max_length=50)
        self.add_item(self.name)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            await self.cog.store.rename_pool(
                interaction.guild_id, self.pool_id, self.name.value.strip()
            )
        except (PoolNameTaken, KeyPoolError) as exc:
            await interaction.response.send_message(embed=error_embed(str(exc)), ephemeral=True)
            return
        await self.cog._show_pool_detail(interaction, self.pool_id)


class NewPoolImportModal(discord.ui.Modal):
    def __init__(self, cog: AccessKeysCog, parsed: ParsedKeys) -> None:
        super().__init__(title=cog.t_locale(discord.Locale.american_english, "keys.import.new_pool_title"))
        self.cog = cog
        self.parsed = parsed
        self.name = discord.ui.TextInput(label="Pool name", max_length=50)
        self.note = discord.ui.TextInput(label="Note", required=False, max_length=200)
        self.add_item(self.name)
        self.add_item(self.note)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            pool = await self.cog.store.create_pool(
                interaction.guild_id,
                name=self.name.value.strip(),
                note=self.note.value.strip() or None,
                created_by=interaction.user.id,
            )
        except (PoolNameTaken, KeyPoolError) as exc:
            await interaction.response.send_message(embed=error_embed(str(exc)), ephemeral=True)
            return
        await self.cog._import_into(interaction, self.parsed, pool.id)


class DropPoolView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, summaries) -> None:
        super().__init__(cog, author_id, locale)
        options = [
            discord.SelectOption(
                label=item.pool.name[:100],
                value=str(item.pool.id),
                description=f"{item.stats.available}",
            )
            for item in summaries[:25]
        ]
        select = discord.ui.Select(
            placeholder=self.label("keys.drop.select_pools"),
            min_values=1,
            max_values=min(10, len(options)),
            options=options,
        )
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        pool_ids = [int(value) for value in values]
        if pool_ids:
            await self.cog._show_drop_setup(interaction, pool_ids)
        else:
            await interaction.response.defer()

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_main(interaction, edit=True)


class DropSetupView(PanelView):
    def __init__(
        self,
        cog: AccessKeysCog,
        author_id: int,
        locale: Any,
        *,
        pool_ids: list[int],
        available: int,
        roles: list[discord.Role],
    ) -> None:
        super().__init__(cog, author_id, locale)
        self.pool_ids = pool_ids
        self.available = available
        self.mode = MODE_RAFFLE
        self.count_mode = COUNT_FOLLOW
        self.key_count: int | None = None
        self.role_id: int | None = None
        self.deny_role_id: int | None = None
        self.minutes = 10
        self.auto_entry = True
        self.role_select = _role_select(self.label("keys.drop.allow_role"), roles, row=0)
        self.deny_select = _role_select(self.label("keys.drop.deny_role"), roles, row=1)
        self.role_select.callback = self._role_picked
        self.deny_select.callback = self._deny_picked
        self.add_item(self.role_select)
        self.add_item(self.deny_select)
        self.mode_button = discord.ui.Button(label="Mode", style=discord.ButtonStyle.primary, row=2)
        self.count_button = discord.ui.Button(label="Count", style=discord.ButtonStyle.secondary, row=2)
        self.minutes_button = discord.ui.Button(label="Minutes", style=discord.ButtonStyle.secondary, row=2)
        self.create_button = discord.ui.Button(
            label=self.label("keys.drop.create"), style=discord.ButtonStyle.success, row=2
        )
        self.back_button = discord.ui.Button(
            label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary, row=2
        )
        self.entry_button = discord.ui.Button(
            label=self.label("keys.drop.entry_on"), style=discord.ButtonStyle.secondary, row=3
        )
        self.mode_button.callback = self._toggle_mode
        self.count_button.callback = self._count
        self.minutes_button.callback = self._minutes
        self.create_button.callback = self._confirm
        self.back_button.callback = self._back
        self.entry_button.callback = self._toggle_entry
        for button in (
            self.mode_button,
            self.count_button,
            self.minutes_button,
            self.create_button,
            self.back_button,
            self.entry_button,
        ):
            self.add_item(button)
        self._sync()

    def _sync(self) -> None:
        self.mode_button.label = (
            self.label("keys.mode.raffle") if self.mode == MODE_RAFFLE else self.label("keys.mode.fcfs")
        )
        self.count_button.label = (
            self.label("keys.count.follow")
            if self.count_mode == COUNT_FOLLOW
            else self.label("keys.count.fixed", count=self.key_count or 0)
        )
        self.minutes_button.disabled = self.mode != MODE_RAFFLE
        self.minutes_button.label = self.label("keys.drop.minutes", minutes=self.minutes)
        self.entry_button.label = self.label(
            "keys.drop.entry_on" if self.auto_entry else "keys.drop.entry_off"
        )

    async def _toggle_entry(self, interaction: discord.Interaction) -> None:
        self.auto_entry = not self.auto_entry
        self._sync()
        await self._refresh(interaction)

    async def _role_picked(self, interaction: discord.Interaction) -> None:
        self.role_id = _selected_role(interaction)
        self._sync()
        await self._refresh(interaction)

    async def _deny_picked(self, interaction: discord.Interaction) -> None:
        self.deny_role_id = _selected_role(interaction)
        self._sync()
        await self._refresh(interaction)

    async def _toggle_mode(self, interaction: discord.Interaction) -> None:
        self.mode = MODE_FCFS if self.mode == MODE_RAFFLE else MODE_RAFFLE
        self._sync()
        await self._refresh(interaction)

    async def _count(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(CountModal(self.cog, self, self.available))

    async def _minutes(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(MinutesModal(self.cog, self))

    async def _confirm(self, interaction: discord.Interaction) -> None:
        await self.cog._create_drop(
            interaction,
            pool_ids=self.pool_ids,
            mode=self.mode,
            count_mode=self.count_mode,
            key_count=self.key_count,
            role_id=self.role_id,
            deny_role_id=self.deny_role_id,
            minutes=self.minutes,
            auto_entry=self.auto_entry,
        )

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._start_drop(interaction)

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(
            embed=self.cog._drop_setup_embed(interaction, self), view=self
        )


def _role_select(placeholder: str, roles: list[discord.Role], *, row: int) -> discord.ui.Select:
    options = [discord.SelectOption(label="—", value="0")]
    options.extend(
        discord.SelectOption(label=role.name[:100], value=str(role.id)) for role in roles[:24]
    )
    return discord.ui.Select(placeholder=placeholder, options=options, row=row)


def _selected_role(interaction: discord.Interaction) -> int | None:
    values = interaction.data.get("values", []) if interaction.data else []
    if not values or values[0] == "0":
        return None
    return int(values[0])


class CountModal(discord.ui.Modal):
    def __init__(self, cog: AccessKeysCog, view: DropSetupView, available: int) -> None:
        super().__init__(title="Count")
        self.cog = cog
        self.view_ref = view
        self.available = available
        self.input = discord.ui.TextInput(
            label="Count (blank = follow stock)",
            required=False,
            max_length=10,
        )
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        raw = self.input.value.strip()
        if not raw:
            self.view_ref.count_mode = COUNT_FOLLOW
            self.view_ref.key_count = None
        else:
            try:
                count = int(raw)
            except ValueError:
                await interaction.response.send_message("Not a number", ephemeral=True)
                return
            if count < 1:
                await interaction.response.send_message("Must be >= 1", ephemeral=True)
                return
            self.view_ref.count_mode = COUNT_FIXED
            self.view_ref.key_count = count
        self.view_ref._sync()
        await interaction.response.edit_message(
            embed=self.cog._drop_setup_embed(interaction, self.view_ref), view=self.view_ref
        )


class MinutesModal(discord.ui.Modal):
    def __init__(self, cog: AccessKeysCog, view: DropSetupView) -> None:
        super().__init__(title="Raffle deadline")
        self.cog = cog
        self.view_ref = view
        self.input = discord.ui.TextInput(label="Minutes", default="10", max_length=4)
        self.add_item(self.input)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        try:
            minutes = int(self.input.value)
        except ValueError:
            await interaction.response.send_message("Not a number", ephemeral=True)
            return
        if minutes < 1 or minutes > 1440:
            await interaction.response.send_message("1-1440", ephemeral=True)
            return
        self.view_ref.minutes = minutes
        self.view_ref._sync()
        await interaction.response.edit_message(
            embed=self.cog._drop_setup_embed(interaction, self.view_ref), view=self.view_ref
        )


class RaffleCardView(discord.ui.View):
    def __init__(self, cog: AccessKeysCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        label = cog.bot.i18n.t(cog.bot.settings.default_locale, "keys.card.enter")
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id="keydrop:raffle")
        button.callback = self._click
        self.add_item(button)

    async def _click(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_raffle(interaction)


class FcfsCardView(discord.ui.View):
    def __init__(self, cog: AccessKeysCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        label = cog.bot.i18n.t(cog.bot.settings.default_locale, "keys.card.claim")
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.success, custom_id="keydrop:fcfs")
        button.callback = self._click
        self.add_item(button)

    async def _click(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_fcfs(interaction)


class RetryView(discord.ui.View):
    def __init__(self, cog: AccessKeysCog) -> None:
        super().__init__(timeout=None)
        self.cog = cog
        label = cog.bot.i18n.t(cog.bot.settings.default_locale, "keys.card.retry")
        button = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, custom_id="keydrop:retry")
        button.callback = self._click
        self.add_item(button)

    async def _click(self, interaction: discord.Interaction) -> None:
        await self.cog._handle_retry(interaction)


class ActiveListView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, drops: list[KeyDrop]) -> None:
        super().__init__(cog, author_id, locale)
        options = [
            discord.SelectOption(
                label=drop.code,
                value=drop.code,
                description=f"{drop.delivered} / {drop.status}",
            )
            for drop in drops[:25]
        ]
        select = discord.ui.Select(placeholder=self.label("keys.active.select"), options=options)
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        drop = await self.cog.store.get_drop(interaction.guild_id, values[0])
        if drop is None:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "keys.drop.missing")), ephemeral=True
            )
            return
        await self.cog._show_active_detail(interaction, drop)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_main(interaction, edit=True)


class ActiveDetailView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, drop: KeyDrop) -> None:
        super().__init__(cog, author_id, locale)
        self.drop = drop
        add = discord.ui.Button(label=self.label("keys.active.add_entry"), style=discord.ButtonStyle.primary)
        add.callback = self._add
        add_pool = discord.ui.Button(label=self.label("keys.active.add_pool"), style=discord.ButtonStyle.secondary)
        add_pool.callback = self._add_pool
        remove_pool = discord.ui.Button(
            label=self.label("keys.active.remove_pool"), style=discord.ButtonStyle.secondary
        )
        remove_pool.callback = self._remove_pool
        manage = discord.ui.Button(
            label=self.label("keys.active.manage_entries"), style=discord.ButtonStyle.secondary
        )
        manage.callback = self._manage
        close = discord.ui.Button(label=self.label("keys.active.close"), style=discord.ButtonStyle.danger)
        close.callback = self._close
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        for button in (add, add_pool, remove_pool, manage, close, back):
            self.add_item(button)

    async def _add(self, interaction: discord.Interaction) -> None:
        await self.cog._add_entry(interaction, self.drop)

    async def _add_pool(self, interaction: discord.Interaction) -> None:
        await self.cog._show_add_pool(interaction, self.drop)

    async def _remove_pool(self, interaction: discord.Interaction) -> None:
        await self.cog._show_remove_pool(interaction, self.drop)

    async def _manage(self, interaction: discord.Interaction) -> None:
        await self.cog._show_entries(interaction, self.drop)

    async def _close(self, interaction: discord.Interaction) -> None:
        view = ConfirmActionView(
            self.cog,
            interaction.user.id,
            interaction.locale,
            prompt=self.cog.t(interaction, "keys.active.close_confirm", code=self.drop.code),
            action=self._do_close,
        )
        await interaction.response.send_message(embed=warn_embed(view.prompt), view=view, ephemeral=True)

    async def _do_close(self, interaction: discord.Interaction) -> None:
        await self.cog._close_drop(interaction, self.drop)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_active(interaction)


class AddPoolView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, drop: KeyDrop, summaries) -> None:
        super().__init__(cog, author_id, locale)
        self.drop = drop
        options = [
            discord.SelectOption(
                label=item.pool.name[:100],
                value=str(item.pool.id),
                description=f"{item.stats.available}",
            )
            for item in summaries[:25]
        ]
        select = discord.ui.Select(placeholder=self.label("keys.active.pick_pool"), options=options)
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        try:
            await self.cog.store.add_pool_to_drop(interaction.guild_id, self.drop.id, int(values[0]))
        except (DropError, KeyPoolError) as exc:
            await interaction.response.send_message(embed=error_embed(str(exc)), ephemeral=True)
            return
        await self.cog._show_active_detail(interaction, self.drop)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_active_detail(interaction, self.drop)


class RemovePoolView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, drop: KeyDrop, pools) -> None:
        super().__init__(cog, author_id, locale)
        self.drop = drop
        options = [
            discord.SelectOption(label=pool.name[:100], value=str(pool.id)) for pool in pools[:25]
        ]
        select = discord.ui.Select(placeholder=self.label("keys.active.pick_pool"), options=options)
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        try:
            await self.cog.store.remove_pool_from_drop(interaction.guild_id, self.drop.id, int(values[0]))
        except (DropError, KeyPoolError) as exc:
            await interaction.response.send_message(embed=error_embed(str(exc)), ephemeral=True)
            return
        await self.cog._show_active_detail(interaction, self.drop)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_active_detail(interaction, self.drop)


class EntryListView(PanelView):
    def __init__(self, cog: AccessKeysCog, author_id: int, locale: Any, drop: KeyDrop, cards) -> None:
        super().__init__(cog, author_id, locale)
        self.drop = drop
        options = []
        for card in cards[:25]:
            channel = cog.bot.get_channel(card.channel_id)
            name = getattr(channel, "name", str(card.channel_id))
            options.append(
                discord.SelectOption(
                    label=f"#{name}"[:100],
                    value=str(card.id),
                    description=str(card.message_id or ""),
                )
            )
        select = discord.ui.Select(placeholder=self.label("keys.active.pick_entry"), options=options)
        select.callback = self._picked
        self.add_item(select)
        back = discord.ui.Button(label=self.label("keys.button.back"), style=discord.ButtonStyle.secondary)
        back.callback = self._back
        self.add_item(back)

    async def _picked(self, interaction: discord.Interaction) -> None:
        values = interaction.data.get("values", []) if interaction.data else []
        if not values:
            return
        card_id = int(values[0])
        cards = await self.cog.store.cards(self.drop.id)
        card = next((item for item in cards if item.id == card_id), None)
        if card is None:
            await interaction.response.send_message(
                embed=error_embed(self.cog.t(interaction, "keys.active.entry_missing")), ephemeral=True
            )
            return
        await self.cog.store.set_card_status(card.id, CARD_CLOSED)
        channel = interaction.guild.get_channel(card.channel_id) if interaction.guild else None
        if isinstance(channel, discord.abc.Messageable) and card.message_id is not None:
            try:
                message = await channel.fetch_message(card.message_id)
                await message.edit(view=None)
            except discord.HTTPException:
                pass
        await self.cog._show_active_detail(interaction, self.drop)

    async def _back(self, interaction: discord.Interaction) -> None:
        await self.cog._show_active_detail(interaction, self.drop)


__all__ = ["AccessKeysCog"]
