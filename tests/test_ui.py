"""确认按钮：只有发起者能点，超时按「取消」处理。"""

from __future__ import annotations

from types import SimpleNamespace

import discord

from bot.core.ui import (
    PAGE_CHAR_LIMIT,
    ConfirmView,
    PageView,
    ask_confirmation,
    paginate_lines,
    send_paged_lines,
)


class FakeResponse:
    def __init__(self) -> None:
        self.messages: list[tuple[str | None, dict]] = []
        self.deferred = False

    async def send_message(self, content: str | None = None, **kwargs) -> None:
        self.messages.append((content, kwargs))

    async def defer(self, **_kwargs) -> None:
        self.deferred = True


class FakeInteraction:
    def __init__(self, user_id: int) -> None:
        self.user = SimpleNamespace(id=user_id)
        self.response = FakeResponse()
        self.edits: list[dict] = []

    async def edit_original_response(self, **kwargs) -> None:
        self.edits.append(kwargs)


def make_view(author_id: int = 1, *, timeout: float = 60.0) -> ConfirmView:
    return ConfirmView(
        author_id=author_id,
        confirm_label="OK",
        cancel_label="No",
        not_author_message="not yours",
        timeout=timeout,
    )


async def test_author_passes_the_check() -> None:
    view = make_view(author_id=1)

    assert await view.interaction_check(FakeInteraction(1)) is True


async def test_other_user_is_rejected_and_told_why() -> None:
    view = make_view(author_id=1)
    interaction = FakeInteraction(2)

    assert await view.interaction_check(interaction) is False
    assert interaction.response.messages == [("not yours", {"ephemeral": True})]


async def test_confirm_resolves_immediately() -> None:
    view = make_view()
    interaction = FakeInteraction(1)

    await view.children[0].callback(interaction)

    assert view.result is True
    assert await view.wait_for_answer() is True
    assert interaction.response.deferred is True


async def test_cancel_resolves_immediately() -> None:
    view = make_view()
    interaction = FakeInteraction(1)

    await view.children[1].callback(interaction)

    assert view.result is False
    assert await view.wait_for_answer() is False


async def test_clicking_before_waiting_does_not_hang() -> None:
    """回归测试：discord.py 的 View.wait() 在这种情况下会永久挂起。"""
    view = make_view()
    await view.children[0].callback(FakeInteraction(1))

    assert await view.wait_for_answer() is True


def test_disable_all_turns_buttons_off() -> None:
    view = make_view()

    view.disable_all()

    assert all(item.disabled for item in view.children)


async def test_timeout_yields_no_answer_and_disables_buttons() -> None:
    view = make_view(timeout=0.01)

    answer = await view.wait_for_answer()

    assert answer is None
    assert view.result is None
    assert all(item.disabled for item in view.children)


async def test_ask_confirmation_returns_false_when_nobody_clicks() -> None:
    view = make_view(timeout=0.01)

    confirmed = await ask_confirmation(FakeInteraction(1), embed=discord.Embed(), view=view)

    assert confirmed is False
    assert all(item.disabled for item in view.children)


async def test_ask_confirmation_returns_true_after_confirm() -> None:
    view = make_view(timeout=5.0)
    await view.children[0].callback(FakeInteraction(1))

    confirmed = await ask_confirmation(FakeInteraction(1), embed=discord.Embed(), view=view)

    assert confirmed is True


# ---------------------------------------------------------------- 分页


def test_paginate_lines_keeps_every_page_within_the_budget() -> None:
    lines = [f"第 {index} 行" for index in range(300)]

    pages = paginate_lines(lines, limit=100)
    flattened = [line for page in pages for line in page]

    assert flattened == lines, "分页不许丢内容"
    assert len(pages) > 1
    for page in pages:
        assert len("\n".join(page)) <= 100


def test_paginate_lines_always_returns_at_least_one_page() -> None:
    assert paginate_lines([], limit=100) == [[]]


def test_paginate_lines_clips_a_single_line_longer_than_the_budget() -> None:
    """一整行就超过上限时必须截断它 —— 否则那一行永远进不了任何一页。"""
    pages = paginate_lines(["x" * 500], limit=100)

    assert len(pages) == 1
    assert len(pages[0][0]) == 100
    assert pages[0][0].endswith("…")


def test_the_page_budget_stays_below_discords_embed_limit() -> None:
    assert PAGE_CHAR_LIMIT < 4096


def make_page_view(author_id: int = 1, *, pages: int = 3) -> PageView:
    return PageView(
        author_id=author_id,
        pages=[discord.Embed(title=f"p{index}") for index in range(pages)],
        previous_label="prev",
        next_label="next",
        close_label="close",
        not_author_message="not yours",
    )


def test_page_view_starts_on_the_first_page_with_prev_disabled() -> None:
    view = make_page_view()

    assert view.index == 0
    assert view.previous.disabled is True
    assert view.next.disabled is False


async def test_other_user_cannot_flip_pages() -> None:
    view = make_page_view(author_id=1)
    interaction = FakeInteraction(2)

    assert await view.interaction_check(interaction) is False
    assert interaction.response.messages == [("not yours", {"ephemeral": True})]


async def test_next_advances_and_edits_the_message() -> None:
    view = make_page_view()
    interaction = FakeInteraction(1)

    await view.next.callback(interaction)

    assert view.index == 1
    assert interaction.response.deferred is True
    assert interaction.edits == [{"embed": view.pages[1], "view": view}]


async def test_previous_goes_back_and_stops_at_the_first_page() -> None:
    view = make_page_view()
    interaction = FakeInteraction(1)

    await view.previous.callback(interaction)
    assert view.index == 0, "已经在第一页时不该往回退"

    await view.next.callback(interaction)
    await view.previous.callback(interaction)
    assert view.index == 0


async def test_the_last_page_disables_next() -> None:
    view = make_page_view(pages=2)
    interaction = FakeInteraction(1)

    await view.next.callback(interaction)

    assert view.index == 1
    assert view.next.disabled is True
    assert view.previous.disabled is False


async def test_close_drops_the_view_and_disables_buttons() -> None:
    view = make_page_view()
    interaction = FakeInteraction(1)

    await view.close.callback(interaction)

    assert interaction.edits == [{"view": None}]
    assert all(item.disabled for item in view.children)


async def test_page_view_timeout_disables_buttons() -> None:
    view = make_page_view()

    await view.on_timeout()

    assert all(item.disabled for item in view.children)


# ---------------------------------------------------------------- 发送分页


def fake_translator(key: str, **_kwargs) -> str:
    return key


async def test_send_paged_lines_stays_a_single_message_when_it_fits() -> None:
    interaction = FakeInteraction(1)

    await send_paged_lines(interaction, title="标题", lines=["a", "b"], translate=fake_translator, footer="页脚")

    _content, kwargs = interaction.response.messages[-1]
    assert "view" not in kwargs
    assert kwargs["embed"].footer.text == "页脚"


async def test_send_paged_lines_adds_buttons_and_page_numbers_when_it_does_not_fit() -> None:
    interaction = FakeInteraction(1)
    lines = [f"第 {index} 行 " + "x" * 100 for index in range(200)]

    await send_paged_lines(interaction, title="标题", lines=lines, translate=fake_translator, footer="提示")

    _content, kwargs = interaction.response.messages[-1]
    view = kwargs["view"]
    assert isinstance(view, PageView)
    assert len(view.pages) > 1
    assert "common.page.indicator" in kwargs["embed"].footer.text
    assert kwargs["embed"].footer.text.endswith("提示")
