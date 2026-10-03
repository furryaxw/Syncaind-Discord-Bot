"""错误处理：异常到文案的映射，以及回给用户的方式。"""

from __future__ import annotations

import logging
from types import SimpleNamespace

import discord
import pytest
from discord import app_commands
from discord.ext import commands

from bot.core.errors import UserError, _classify, handle_app_command_error, unwrap
from bot.core.i18n import I18n


class FakeResponse:
    def __init__(self, *, done: bool = False) -> None:
        self._done = done
        self.sent: list[tuple[discord.Embed | None, dict]] = []

    def is_done(self) -> bool:
        return self._done

    async def send_message(self, embed: discord.Embed | None = None, **kwargs) -> None:
        self.sent.append((embed, kwargs))


class FakeFollowup:
    def __init__(self) -> None:
        self.sent: list[tuple[discord.Embed | None, dict]] = []

    async def send(self, embed: discord.Embed | None = None, **kwargs) -> None:
        self.sent.append((embed, kwargs))


class FakeInteraction:
    def __init__(self, *, done: bool = False, locale: str = "zh-CN") -> None:
        self.command = "demo"
        self.locale = locale
        self.response = FakeResponse(done=done)
        self.followup = FakeFollowup()


def http_like(status: int, reason: str = "reason") -> SimpleNamespace:
    return SimpleNamespace(status=status, reason=reason)


def test_user_error_keeps_key_and_placeholders() -> None:
    error = UserError("errors.hierarchy.target_is_owner", target="@someone")

    key, kwargs, unexpected = _classify(error)

    assert key == "errors.hierarchy.target_is_owner"
    assert kwargs == {"target": "@someone"}
    assert unexpected is False


@pytest.mark.parametrize(
    ("error", "expected_key"),
    [
        (app_commands.MissingPermissions(["ban_members"]), "errors.missing_permissions"),
        (app_commands.BotMissingPermissions(["manage_roles"]), "errors.bot_missing_permissions"),
        (app_commands.CheckFailure("nope"), "errors.permission_denied"),
        (discord.Forbidden(http_like(403, "Forbidden"), {"message": "x", "code": 50013}), "errors.discord_forbidden"),
        (discord.NotFound(http_like(404), {"message": "x", "code": 10007}), "errors.discord_not_found"),
        (discord.HTTPException(http_like(500), {"message": "x"}), "errors.discord_http"),
        (RuntimeError("boom"), "errors.unexpected"),
    ],
)
def test_error_classification(error: Exception, expected_key: str) -> None:
    key, _kwargs, _unexpected = _classify(error)

    assert key == expected_key


def test_cooldown_reports_the_remaining_seconds() -> None:
    error = app_commands.CommandOnCooldown(commands.Cooldown(1, 12.0), 12.0)

    key, kwargs, unexpected = _classify(error)

    assert key == "errors.cooldown"
    assert kwargs["seconds"] == "12"
    assert unexpected is False


def test_unexpected_errors_are_flagged() -> None:
    _key, _kwargs, unexpected = _classify(RuntimeError("boom"))

    assert unexpected is True


async def test_handler_replies_ephemerally(i18n: I18n, caplog: pytest.LogCaptureFixture) -> None:
    interaction = FakeInteraction()

    with caplog.at_level(logging.ERROR):
        await handle_app_command_error(
            interaction,  # type: ignore[arg-type]
            UserError("errors.hierarchy.actor_role_too_low", target="@someone"),
            i18n=i18n,
            logger=logging.getLogger("test"),
        )

    embed, kwargs = interaction.response.sent[0]
    assert kwargs == {"ephemeral": True}
    assert embed is not None
    assert "@someone" in (embed.description or "")
    assert caplog.records == []


async def test_handler_falls_back_to_followup(i18n: I18n) -> None:
    interaction = FakeInteraction(done=True)

    await handle_app_command_error(
        interaction,  # type: ignore[arg-type]
        UserError("errors.owner_only"),
        i18n=i18n,
        logger=logging.getLogger("test"),
    )

    assert interaction.response.sent == []
    _embed, kwargs = interaction.followup.sent[0]
    assert kwargs == {"ephemeral": True}


async def test_unexpected_error_is_logged_with_traceback(i18n: I18n, caplog: pytest.LogCaptureFixture) -> None:
    interaction = FakeInteraction()

    with caplog.at_level(logging.ERROR):
        await handle_app_command_error(
            interaction,  # type: ignore[arg-type]
            RuntimeError("boom"),
            i18n=i18n,
            logger=logging.getLogger("test.errors"),
        )

    assert any("未预期异常" in record.message for record in caplog.records)
    embed, _kwargs = interaction.response.sent[0]
    assert embed is not None
    assert "boom" not in (embed.description or "")


async def test_english_client_gets_english_message(i18n: I18n) -> None:
    interaction = FakeInteraction(locale="en-US")

    await handle_app_command_error(
        interaction,  # type: ignore[arg-type]
        UserError("errors.owner_only"),
        i18n=i18n,
        logger=logging.getLogger("test"),
    )

    embed, _kwargs = interaction.response.sent[0]
    assert embed is not None
    assert embed.description == "Only the bot owner can use this command."


# ---------------------------------------------------------------- discord.py 的包装
#
# discord.py 会把回调里的异常包成 CommandInvokeError，分类器必须先拆掉这层包装，
# 否则所有「面向用户的一句话」都退化成通用报错并打一条带堆栈的 ERROR。
# 下面这些用例走的就是**真实的投递形状**。


def wrap(error: Exception) -> app_commands.CommandInvokeError:
    """照 discord.py 的构造方式包一层（``command`` 只要有 ``name`` 就行）。"""
    return app_commands.CommandInvokeError(SimpleNamespace(name="claim"), error)


def test_unwrap_peels_the_discord_wrapper() -> None:
    original = UserError("keys.team_unknown", batch="b1")

    assert unwrap(wrap(original)) is original
    assert unwrap(original) is original, "没包装的异常原样返回"
    assert unwrap(wrap(wrap(original))) is original, "嵌套也要拆到底"


@pytest.mark.parametrize(
    ("error", "expected_key"),
    [
        (UserError("errors.owner_only"), "errors.owner_only"),
        (app_commands.CheckFailure("nope"), "errors.permission_denied"),
        (discord.Forbidden(http_like(403, "Forbidden"), {"message": "x", "code": 50013}), "errors.discord_forbidden"),
        (RuntimeError("boom"), "errors.unexpected"),
    ],
)
def test_classification_survives_the_wrapper(error: Exception, expected_key: str) -> None:
    """包一层之后，分类结果必须和不包时一样。"""
    assert _classify(unwrap(wrap(error)))[0] == expected_key


async def test_a_wrapped_user_error_still_reaches_the_user(i18n: I18n, caplog: pytest.LogCaptureFixture) -> None:
    """真机回归：包在 CommandInvokeError 里的 UserError 必须**照原样**给用户看，且不记 ERROR。"""
    interaction = FakeInteraction()

    with caplog.at_level(logging.ERROR):
        await handle_app_command_error(
            interaction,  # type: ignore[arg-type]
            wrap(UserError("keys.team_unknown", batch="b1")),
            i18n=i18n,
            logger=logging.getLogger("test.errors"),
        )

    embed, kwargs = interaction.response.sent[0]
    assert kwargs == {"ephemeral": True}
    assert embed is not None
    assert "b1" in (embed.description or ""), "要看到 UserError 自己的文案"
    assert "未预期异常" not in caplog.text, "这不是未预期异常，不该打 ERROR"


async def test_a_wrapped_unexpected_error_is_still_logged(i18n: I18n, caplog: pytest.LogCaptureFixture) -> None:
    """反过来也要成立：真正没预料到的异常仍然要留堆栈。"""
    interaction = FakeInteraction()

    with caplog.at_level(logging.ERROR):
        await handle_app_command_error(
            interaction,  # type: ignore[arg-type]
            wrap(RuntimeError("boom")),
            i18n=i18n,
            logger=logging.getLogger("test.errors"),
        )

    assert "未预期异常" in caplog.text


async def test_a_deferred_command_error_goes_out_as_a_followup(i18n: I18n) -> None:
    """命令自己 defer 过之后再抛 UserError（比如先调外部接口）—— 提示不能丢。"""
    interaction = FakeInteraction(done=True)

    await handle_app_command_error(
        interaction,  # type: ignore[arg-type]
        wrap(UserError("keys.team_unknown", batch="b1")),
        i18n=i18n,
        logger=logging.getLogger("test"),
    )

    assert interaction.response.sent == []
    embed, _kwargs = interaction.followup.sent[0]
    assert embed is not None and "b1" in (embed.description or "")
