"""启动循环：连接失败要重试，而不是让进程死掉。

``discord.Client.run()`` 的 ``reconnect`` 只管已建立的会话：**首次连接失败会把异常抛出来**，
进程直接以退出码 1 结束 —— 网络抖 20 秒，机器人就再也起不来。
"""

from __future__ import annotations

import logging
from typing import Any

import discord
import pytest

from bot.core.run_loop import EXIT_GAVE_UP, EXIT_OK, RetryPolicy, run_bot


class FakeBot:
    """按脚本依次抛异常或正常返回；记录 token、log_handler 与有没有被关掉。"""

    def __init__(self, *, outcomes: list[Any]) -> None:
        self._outcomes = outcomes
        self.closed = False
        self.token: str | None = None
        self.log_handler: Any = "未传"

    def run(self, token: str, *, log_handler: Any = None) -> None:
        self.token = token
        self.log_handler = log_handler
        outcome = self._outcomes.pop(0)
        if isinstance(outcome, BaseException):
            raise outcome

    async def close(self) -> None:
        self.closed = True


def make_factory(outcomes: list[Any]) -> tuple[Any, list[FakeBot]]:
    created: list[FakeBot] = []

    def factory(*, settings: Any, logger: Any) -> FakeBot:
        bot = FakeBot(outcomes=outcomes)
        created.append(bot)
        return bot

    return factory, created


def make_settings() -> Any:
    return type("S", (), {"discord_token": "tok-1"})()


def run(outcomes: list[Any], **kwargs: Any) -> tuple[int, list[FakeBot], list[float]]:
    sleeps: list[float] = []
    factory, created = make_factory(list(outcomes))
    code = run_bot(
        make_settings(),
        logger=logging.getLogger("test.run_loop"),
        bot_factory=factory,
        sleep=sleeps.append,
        **kwargs,
    )
    return code, created, sleeps


def run_expecting_fatal(outcomes: list[Any]) -> tuple[BaseException, list[FakeBot], list[float]]:
    sleeps: list[float] = []
    factory, created = make_factory(list(outcomes))
    with pytest.raises(BaseException) as excinfo:
        run_bot(
            make_settings(),
            logger=logging.getLogger("test.run_loop"),
            bot_factory=factory,
            sleep=sleeps.append,
        )
    return excinfo.value, created, sleeps


# ---------------------------------------------------------------- 策略


def test_delay_doubles_and_caps() -> None:
    policy = RetryPolicy(initial_seconds=1.0, maximum_seconds=8.0)

    assert [policy.delay_for(n) for n in range(1, 6)] == [1.0, 2.0, 4.0, 8.0, 8.0]


def test_delay_rejects_attempt_zero() -> None:
    with pytest.raises(ValueError):
        RetryPolicy().delay_for(0)


def test_unlimited_retries_by_default() -> None:
    assert RetryPolicy().should_retry(10_000)


# ---------------------------------------------------------------- 正常路径


def test_a_clean_run_exits_zero_without_sleeping() -> None:
    code, created, sleeps = run([None])

    assert code == EXIT_OK
    assert len(created) == 1
    assert sleeps == []
    assert created[0].token == "tok-1"
    assert created[0].log_handler is None, "别让 discord.py 再装一个日志 handler"


def test_keyboard_interrupt_is_a_normal_exit() -> None:
    code, created, sleeps = run([KeyboardInterrupt()])

    assert code == EXIT_OK
    assert sleeps == []
    assert len(created) == 1


# ---------------------------------------------------------------- 重试


def test_a_network_failure_is_retried_and_then_succeeds() -> None:
    """连接被重置一次，第二次成功 —— 进程要活下来。"""
    code, created, sleeps = run([ConnectionResetError("网络抖了"), None])

    assert code == EXIT_OK
    assert len(created) == 2
    assert sleeps == [1.0]
    assert created[0].closed is True, "失败那次占用的资源要尽量收回来"


def test_backoff_grows_between_attempts() -> None:
    policy = RetryPolicy(initial_seconds=1.0, maximum_seconds=4.0, max_attempts=5)

    code, created, sleeps = run([RuntimeError("一直连不上")] * 5, policy=policy)

    assert code == EXIT_GAVE_UP
    assert sleeps == [1.0, 2.0, 4.0, 4.0]
    assert len(created) == 5


def test_every_failed_attempt_creates_a_fresh_bot() -> None:
    """失败的实例不能复用：它的 aiohttp 会话与事件循环都已经关掉了。"""
    code, created, _sleeps = run([ConnectionResetError()] * 3, policy=RetryPolicy(max_attempts=3))

    assert code == EXIT_GAVE_UP
    assert len(created) == 3
    assert len({id(bot) for bot in created}) == 3


def test_interrupt_during_the_backoff_wait_exits_cleanly() -> None:
    def interrupted(_delay: float) -> None:
        raise KeyboardInterrupt

    factory, created = make_factory([ConnectionResetError()])
    code = run_bot(
        make_settings(),
        logger=logging.getLogger("test.run_loop"),
        bot_factory=factory,
        sleep=interrupted,
    )

    assert code == EXIT_OK
    assert len(created) == 1


# ---------------------------------------------------------------- 不该重试的


def test_login_failure_is_not_retried() -> None:
    """token 无效再试一万次也一样，要的是改配置。"""
    error, created, sleeps = run_expecting_fatal([discord.LoginFailure("bad token")])

    assert isinstance(error, discord.LoginFailure)
    assert len(created) == 1
    assert sleeps == []
    assert created[0].closed is True


def test_privileged_intents_failure_is_not_retried() -> None:
    error, created, sleeps = run_expecting_fatal([discord.PrivilegedIntentsRequired(None)])

    assert isinstance(error, discord.PrivilegedIntentsRequired)
    assert len(created) == 1
    assert sleeps == []
