"""界面层：Embed 工厂与危险操作确认按钮。"""

from __future__ import annotations

import asyncio
import logging
from typing import Any

import discord

COLOR_OK = 0x57F287
COLOR_ERROR = 0xED4245
COLOR_WARN = 0xFEE75C
COLOR_INFO = 0x5865F2

DEFAULT_CONFIRM_TIMEOUT = 60.0

_LOGGER = logging.getLogger("bot.ui")


def info_embed(title: str, description: str | None = None) -> discord.Embed:
    embed = discord.Embed(title=title, description=description, color=COLOR_INFO)
    return embed


def ok_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_OK)


def error_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_ERROR)


def warn_embed(title: str, description: str | None = None) -> discord.Embed:
    return discord.Embed(title=title, description=description, color=COLOR_WARN)


def add_fields(embed: discord.Embed, fields: list[tuple[str, Any, bool]]) -> discord.Embed:
    """批量加字段，值为 ``None`` 的跳过。"""
    for name, value, inline in fields:
        if value is None:
            continue
        embed.add_field(name=name, value=str(value), inline=inline)
    return embed


class ConfirmView(discord.ui.View):
    """危险操作的二次确认。

    * 只有发起者本人能点（``interaction_check``）；
    * 超时后按钮失效，``result`` 保持 ``None``（调用方据此判定「已取消」）。

    等待结果用自己持有的 ``asyncio.Event``，而**不是** ``View.wait()``：
    discord.py 的 ``stop()`` 只在内部 future 已经存在时才写结果，因此
    「先 stop 再 wait」会创建一个永远不会被 set 的 future，把命令永久挂住。
    ``stop()`` 早于 ``wait()`` 在真实点击里完全可能发生（点击与等待是并发的）。
    """

    def __init__(
        self,
        *,
        author_id: int,
        confirm_label: str,
        cancel_label: str,
        not_author_message: str = "This confirmation is not yours.",
        timeout: float = DEFAULT_CONFIRM_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        self.author_id = author_id
        self.result: bool | None = None
        self._not_author_message = not_author_message
        self._timeout = timeout
        self._answered = asyncio.Event()

        confirm_button = discord.ui.Button(label=confirm_label, style=discord.ButtonStyle.danger)
        confirm_button.callback = self._on_confirm
        cancel_button = discord.ui.Button(label=cancel_label, style=discord.ButtonStyle.secondary)
        cancel_button.callback = self._on_cancel
        self.add_item(confirm_button)
        self.add_item(cancel_button)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(self._not_author_message, ephemeral=True)
            return False
        return True

    async def wait_for_answer(self) -> bool | None:
        """等答复。返回 ``True`` 表示确认，``False`` 表示取消，``None`` 表示超时。"""
        try:
            await asyncio.wait_for(self._answered.wait(), timeout=self._timeout)
        except asyncio.TimeoutError:
            _LOGGER.debug("确认卡片超时（未被派发的视图也走这条路径）")
        self.disable_all()
        return self.result

    async def on_timeout(self) -> None:
        """discord.py 对「已发出」的视图的超时回调，与 wait_for_answer 的兜底等价。"""
        self.disable_all()
        self._answered.set()

    async def _on_confirm(self, interaction: discord.Interaction) -> None:
        self.result = True
        await interaction.response.defer()
        self.stop()
        self._answered.set()

    async def _on_cancel(self, interaction: discord.Interaction) -> None:
        self.result = False
        await interaction.response.defer()
        self.stop()
        self._answered.set()

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True


async def ask_confirmation(
    interaction: discord.Interaction,
    *,
    embed: discord.Embed,
    view: ConfirmView,
) -> bool:
    """发出确认卡片并等结果。返回 ``True`` 才表示用户确认过。"""
    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)
    return await view.wait_for_answer() is True
