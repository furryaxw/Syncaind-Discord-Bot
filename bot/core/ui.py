"""界面层：Embed 工厂、危险操作确认按钮与翻页视图。"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Sequence
from typing import Any

import discord

COLOR_OK = 0x57F287
COLOR_ERROR = 0xED4245
COLOR_WARN = 0xFEE75C
COLOR_INFO = 0x5865F2

DEFAULT_CONFIRM_TIMEOUT = 60.0

# 一条 embed 的正文上限是 4096 字符。分页时按 3800 装 —— 留出标题与页脚的位置，
# 也留出 Discord 自己算长度时的余量；撞上 4096 是整条命令失败，不是少显示几个字。
PAGE_CHAR_LIMIT = 3800
DEFAULT_PAGE_TIMEOUT = 120.0

# 单个 embed 字段值的上限。往字段里塞名单时按它截断，否则整条消息会被拒收。
FIELD_VALUE_LIMIT = 1024

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


def clip_text(value: str, limit: int) -> str:
    """按上限截断并留一个省略号。**只在「已经另有地方能看全文」时用**。"""
    return value if len(value) <= limit else value[: limit - 1] + "…"


def paginate_lines(lines: Sequence[str], *, limit: int = PAGE_CHAR_LIMIT) -> list[list[str]]:
    """把行按字符预算切成页。**至少返回一页**（通常是空页），调用方不必先判空。

    这不是「好看的排版」而是可用的底线：一条 embed 正文超过 4096 字符，
    Discord 会拒收整条消息，用户看到的是「接口报错」，本来想看的东西一个字也看不到。
    单行本身就超过 ``limit`` 时截断它 —— 否则那一行永远放不进任何一页。
    """
    pages: list[list[str]] = []
    current: list[str] = []
    used = 0
    for line in lines:
        text = line if len(line) <= limit else line[: limit - 1] + "…"
        size = len(text) + 1  # 行尾换行
        if current and used + size > limit:
            pages.append(current)
            current, used = [], 0
        current.append(text)
        used += size
    pages.append(current)
    return pages


class PageView(discord.ui.View):
    """翻页按钮：上一页 / 下一页 / 关闭。**只有发起者能点**（同 :class:`ConfirmView`）。

    一页一个 embed。回调先 ``defer`` 再 ``edit_original_response``：真机上组件交互同样受
    3 秒窗口约束，先响应再改消息才能把「内容多、渲染慢」和「交互失败」分开。
    """

    def __init__(
        self,
        *,
        author_id: int,
        pages: Sequence[discord.Embed | str],
        previous_label: str,
        next_label: str,
        close_label: str,
        not_author_message: str,
        timeout: float = DEFAULT_PAGE_TIMEOUT,
    ) -> None:
        super().__init__(timeout=timeout)
        if not pages:
            raise ValueError("翻页视图至少要有一页")
        self.author_id = author_id
        self.pages = list(pages)
        self.index = 0
        self._not_author_message = not_author_message

        self.previous = discord.ui.Button(label=previous_label, style=discord.ButtonStyle.secondary)
        self.next = discord.ui.Button(label=next_label, style=discord.ButtonStyle.secondary)
        self.close = discord.ui.Button(label=close_label, style=discord.ButtonStyle.danger)
        self.previous.callback = self._go_previous
        self.next.callback = self._go_next
        self.close.callback = self._close
        for item in (self.previous, self.next, self.close):
            self.add_item(item)
        self._sync_buttons()

    @property
    def page(self) -> discord.Embed | str:
        return self.pages[self.index]

    def _sync_buttons(self) -> None:
        self.previous.disabled = self.index == 0
        self.next.disabled = self.index >= len(self.pages) - 1

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.author_id:
            await interaction.response.send_message(self._not_author_message, ephemeral=True)
            return False
        return True

    async def _go_previous(self, interaction: discord.Interaction) -> None:
        if self.index > 0:
            self.index -= 1
        await self._render(interaction)

    async def _go_next(self, interaction: discord.Interaction) -> None:
        if self.index < len(self.pages) - 1:
            self.index += 1
        await self._render(interaction)

    async def _render(self, interaction: discord.Interaction) -> None:
        self._sync_buttons()
        await interaction.response.defer()
        await interaction.edit_original_response(embed=self.page, view=self)

    async def _close(self, interaction: discord.Interaction) -> None:
        self.disable_all()
        await interaction.response.defer()
        await interaction.edit_original_response(view=None)

    def disable_all(self) -> None:
        for item in self.children:
            if isinstance(item, discord.ui.Button):
                item.disabled = True

    async def on_timeout(self) -> None:
        """超时后按钮在本地失效（真机上再点会得到「交互失败」），与 ConfirmView 一致。"""
        self.disable_all()


async def reply_with_pages(interaction: discord.Interaction, view: PageView, *, ephemeral: bool = True) -> None:
    """把分页视图作为本次响应发出去（第一页）。调用方自己造好每一页的 embed。"""
    await interaction.response.send_message(embed=view.page, view=view, ephemeral=ephemeral)


async def send_paged_lines(
    interaction: discord.Interaction,
    *,
    title: str,
    lines: Sequence[str],
    translate: Callable[..., str],
    footer: str | None = None,
    ephemeral: bool = True,
) -> None:
    """把一堆行分页发出：装得下就一条 embed，装不下就带翻页按钮。

    ``translate`` 是绑定好语言的查文案函数（``key, **kwargs -> str``）：分页按钮与页脚
    用的是核心文案 ``common.page.*``。``footer`` 单页时就是页脚，多页时与页码并排。

    这是「列表命令」的统一出口：任何一行都不许把整条 embed 撑过 4096 —— 那会让命令
    整体报错，用户连前面几条都看不到。
    """
    pages = paginate_lines(lines)
    embeds = [info_embed(title, "\n".join(page) or None) for page in pages]
    total = len(embeds)
    if total == 1:
        if footer:
            embeds[0].set_footer(text=footer)
        await interaction.response.send_message(embed=embeds[0], ephemeral=ephemeral)
        return

    for index, embed in enumerate(embeds, start=1):
        indicator = translate("common.page.indicator", page=index, total=total)
        embed.set_footer(text=f"{indicator}　{footer}" if footer else indicator)
    view = PageView(
        author_id=interaction.user.id,
        pages=embeds,
        previous_label=translate("common.page.previous"),
        next_label=translate("common.page.next"),
        close_label=translate("common.page.close"),
        not_author_message=translate("common.page.not_yours"),
    )
    await reply_with_pages(interaction, view, ephemeral=ephemeral)
