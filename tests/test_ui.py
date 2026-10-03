"""确认按钮：只有发起者能点，超时按「取消」处理。"""

from __future__ import annotations

from types import SimpleNamespace

import discord

from bot.core.ui import ConfirmView, ask_confirmation


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
