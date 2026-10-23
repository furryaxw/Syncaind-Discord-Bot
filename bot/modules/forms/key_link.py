"""表单 ↔ key 活动：把一次提交变成 N 枚 key 的领取。

只依赖 `bot.services["keys"]`，不 import access_keys 的内部实现；access_keys 停用时
这里会明确报「不可用」，表单其余功能不受影响。
"""

from __future__ import annotations

import logging
from typing import Any

import discord

from bot.core.clock import utcnow_iso
from bot.core.errors import UserError
from bot.integrations.keypool import MODE_FCFS
from bot.modules.forms.schema import FormDefinition
from bot.modules.forms.store import FormBinding, FormSubmission

LOGGER = logging.getLogger("bot.forms")
MAX_MESSAGE_CHARS = 1900


class KeyLinkError(UserError):
    """key 联动不可用/库存不足/投递失败。"""


class FormKeyLinker:
    def __init__(self, bot: Any, *, logger: logging.Logger | None = None) -> None:
        self.bot = bot
        self._logger = logger or LOGGER

    async def available(self, guild_id: int, binding: FormBinding) -> int:
        service = self.bot.services.get("keys")
        if service is None or binding.drop_id is None:
            return 0
        return await service.available_for_drop(guild_id, binding.drop_id)

    async def issue(
        self,
        guild: discord.Guild,
        definition: FormDefinition,
        binding: FormBinding,
        submission: FormSubmission,
    ) -> list[str]:
        count = definition.grant_key
        if count <= 0:
            return []
        if binding.drop_id is None:
            raise KeyLinkError("forms.key.no_drop")
        service = self.bot.services.get("keys")
        if service is None:
            raise KeyLinkError("forms.key.unavailable")
        drop = await service.drop(guild.id, binding.drop_id)
        if drop is None or drop.status == "closed":
            raise KeyLinkError("forms.key.no_drop")
        if drop.mode != MODE_FCFS:
            raise KeyLinkError("forms.key.not_fcfs")
        pools = await service.drop_pools(drop.id)
        pool_ids = [pool.id for pool in pools]
        available = await service.available(guild.id, pool_ids)
        if available < count:
            raise KeyLinkError("forms.key.no_stock")

        reservations = []
        for _ in range(count):
            reservation = await service.reserve(
                guild.id, pool_ids, user_id=submission.discord_user_id, drop_id=drop.id
            )
            if reservation is None:
                for held in reservations:
                    await service.release(held)
                raise KeyLinkError("forms.key.no_stock")
            reservations.append(reservation)

        keys = [reservation.secret for reservation in reservations]
        body = "\n".join(keys)
        if len(body) > MAX_MESSAGE_CHARS:
            for reservation in reservations:
                await service.release(reservation)
            raise KeyLinkError("forms.key.too_long")

        member = guild.get_member(submission.discord_user_id)
        if member is None:
            for reservation in reservations:
                await service.release(reservation)
            raise KeyLinkError("forms.key.member_gone")
        locale = await self._locale(guild.id)
        try:
            await member.send(self.bot.i18n.t(locale, "forms.key.dm", count=count, keys=body))
        except discord.HTTPException:
            for reservation in reservations:
                await service.release(reservation)
            raise KeyLinkError("forms.key.dm_failed") from None

        for reservation in reservations:
            await service.commit(reservation)
            await self.bot.db.execute(
                """
                INSERT OR IGNORE INTO form_key_grants
                    (submission_id, key_id, pool_id, delivered_at)
                VALUES (?, ?, ?, ?)
                """,
                (submission.submission_id, reservation.key_id, reservation.pool_id, utcnow_iso()),
            )
        self._logger.info(
            "表单发 key：submission=%s count=%s drop=%s", submission.submission_id, count, drop.code
        )
        return keys

    async def _locale(self, guild_id: int) -> str:
        settings = await self.bot.guild_settings.get(guild_id)
        return settings.locale or self.bot.settings.default_locale
