"""GitHub web flow：授权链接、state、回调端点，以及回调真的把绑定写进库里。

回调那几条用 ``aiohttp.test_utils`` 起一个**真的本地 socket**（只绑 127.0.0.1 的临时端口），
所以路由、分发、查询参数解析、HTML 响应全都是真跑的——不是拿假对象糊过去。
"""

from __future__ import annotations

import logging
import socket
from typing import Any

import discord
import pytest
from aiohttp.test_utils import TestClient, TestServer

from bot.core.errors import UserError
from bot.core.web import BotWebServer, minimal_page
from bot.integrations.github import GitHubAccountStore, StateStore
from bot.integrations.github.client import GitHubClient, WebFlowError
from tests.discord_fakes import (
    GUILD_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    build_guild,
    run_command,
    setup_bot,
)
from tests.github_fakes import FakeTransport

REDIRECT_URI = "https://example.ts.net/github/callback"
CALLBACK_PATH = "/github/callback"
CLIENT_ID = "cid-123"
CLIENT_SECRET = "secret-456"
GITHUB_ID = 42
GITHUB_LOGIN = "furryaxw"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def make_client(transport: FakeTransport) -> GitHubClient:
    return GitHubClient(
        CLIENT_ID,
        transport,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        user_agent="TestAgent",
    )


def make_interaction(guild: Any, **kwargs: Any) -> FakeInteraction:
    return FakeInteraction(user=guild.get_member(MODERATOR_ID), guild=guild, **kwargs)


def patch_guild(bot: Any, guild: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(bot, "get_guild", lambda guild_id: guild)


def enable_endpoint(bot: Any, monkeypatch: pytest.MonkeyPatch) -> None:
    """把「入站端点正在监听」这件事装出来——真实监听另有用例覆盖。"""
    monkeypatch.setattr(type(bot.web), "is_running", property(lambda self: True))


def configure_web_flow(bot: Any) -> None:
    bot.settings.github_oauth_client_id = CLIENT_ID
    bot.settings.github_oauth_client_secret = CLIENT_SECRET
    bot.settings.github_oauth_redirect_uri = REDIRECT_URI


def last_message(interaction: FakeInteraction) -> discord.Embed:
    return interaction.response._messages[-1]["embed"]


# ---------------------------------------------------------------- 客户端


async def test_authorize_url_carries_what_github_needs() -> None:
    url = make_client(FakeTransport()).authorize_url("state-1")

    assert url.startswith("https://github.com/login/oauth/authorize?")
    assert f"client_id={CLIENT_ID}" in url
    assert "state=state-1" in url
    assert "scope=read" in url
    assert "redirect_uri=https%3A%2F%2Fexample.ts.net%2Fgithub%2Fcallback" in url


def test_authorize_url_needs_a_redirect_uri() -> None:
    client = GitHubClient(CLIENT_ID, FakeTransport(), client_secret=CLIENT_SECRET)

    with pytest.raises(WebFlowError):
        client.authorize_url("state-1")


def test_web_flow_needs_all_three_settings() -> None:
    transport = FakeTransport()

    assert GitHubClient(CLIENT_ID, transport, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI).can_use_web_flow
    assert not GitHubClient(CLIENT_ID, transport, redirect_uri=REDIRECT_URI).can_use_web_flow
    assert not GitHubClient(CLIENT_ID, transport, client_secret=CLIENT_SECRET).can_use_web_flow
    assert not GitHubClient("", transport, client_secret=CLIENT_SECRET, redirect_uri=REDIRECT_URI).can_use_web_flow


async def test_exchange_code_trades_the_code_in_and_returns_the_user() -> None:
    transport = FakeTransport(polls=[{"access_token": "tok-1"}])

    user = await make_client(transport).exchange_code("code-1")

    assert user.id == 42
    assert user.login == GITHUB_LOGIN
    _method, _url, data = transport.calls[0]
    assert data["client_secret"] == CLIENT_SECRET
    assert data["code"] == "code-1"
    assert data["client_id"] == CLIENT_ID
    # token 只在函数内部用来调 /user，不往上层冒
    assert "tok-1" not in repr(user)


async def test_exchange_code_raises_on_an_error_payload() -> None:
    transport = FakeTransport(polls=[{"error": "bad_verification_code", "error_description": "nope"}])

    with pytest.raises(WebFlowError) as excinfo:
        await make_client(transport).exchange_code("code-1")

    assert excinfo.value.code == "bad_verification_code"


# ---------------------------------------------------------------- state


def test_state_is_random_and_single_use() -> None:
    store = StateStore()

    first = store.create(guild_id=1, discord_user_id=2)
    second = store.create(guild_id=1, discord_user_id=3)

    assert first.state != second.state
    assert len(first.state) >= 32
    assert store.consume(first.state) == first
    assert store.consume(first.state) is None, "state 必须一次性"
    assert store.consume("guess") is None


def test_state_expires() -> None:
    now = [1000.0]
    store = StateStore(ttl_seconds=60, clock=lambda: now[0])
    pending = store.create(guild_id=1, discord_user_id=2)

    now[0] += 61

    assert store.consume(pending.state) is None


def test_expired_states_are_purged_on_the_next_create() -> None:
    now = [1000.0]
    store = StateStore(ttl_seconds=60, clock=lambda: now[0])
    store.create(guild_id=1, discord_user_id=2)

    now[0] += 61
    store.create(guild_id=1, discord_user_id=3)

    assert len(store) == 1


# ---------------------------------------------------------------- 入站端点本身


def test_server_is_disabled_without_a_port() -> None:
    server = BotWebServer(port=0)

    assert server.is_enabled is False
    assert server.is_running is False


async def test_server_serves_only_registered_routes() -> None:
    """真的起一个本地 socket：路由、分发、404 都跑一遍。"""
    server = BotWebServer(port=free_port())

    async def handler(request: Any) -> Any:
        from aiohttp import web

        return web.Response(text="ok")

    server.add_route("GET", "/hello", handler)
    await server.start()
    try:
        async with TestClient(TestServer(server.build_app())) as client:
            assert (await client.get("/hello")).status == 200
            assert (await client.get("/nope")).status == 404
            assert (await client.post("/hello")).status == 404, "方法也要对得上"
    finally:
        await server.stop()


def test_duplicate_routes_are_refused() -> None:
    server = BotWebServer(port=0)
    server.add_route("GET", "/x", lambda request: None)  # type: ignore[arg-type]

    with pytest.raises(ValueError):
        server.add_route("GET", "/x", lambda request: None)  # type: ignore[arg-type]


async def test_a_binding_error_does_not_take_the_bot_down(
    caplog: pytest.LogCaptureFixture,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """绑定失败（真实触发就是端口被占用）时，端点起不来而已。

    这一条重要：``start()`` 抛出的 OSError 会顺着 ``prepare()`` 冒到 ``bot.run()``，
    于是「机器人起不来 + 退避重试永远重试」——一个端口冲突不该有这种后果。

    这里直接注入绑定失败，而不是抢一个端口：靠 ``free_port()`` 那种写法本身有竞态，
    而且不同平台对「同一端口能否重复绑定」的默认行为并不一致。
    """
    from aiohttp import web

    async def refuse(self: Any, *args: Any, **kwargs: Any) -> None:
        raise OSError(10048, "address already in use")

    monkeypatch.setattr(web.TCPSite, "start", refuse)
    server = BotWebServer(port=free_port())

    with caplog.at_level(logging.ERROR, logger="bot.web"):
        assert await server.start() is False

    assert server.is_running is False
    assert "入站端点没起来" in caplog.text


def test_pages_escape_their_arguments() -> None:
    """页面里可能出现外部字符串（GitHub 登录名），必须转义。"""
    html = minimal_page("<script>", "<img src=x onerror=alert(1)>")

    assert "<script>" not in html
    assert "&lt;script&gt;" in html
    # 关键是不能以「标签」的形式出现——转义之后 onerror 只是普通文本，戳不动任何东西
    assert "<img" not in html
    assert "&lt;img src=x" in html


# ---------------------------------------------------------------- /link github 的选择逻辑


async def test_web_method_says_what_is_missing(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        bot.settings.github_oauth_client_id = CLIENT_ID
        guild = build_guild()
        interaction = make_interaction(guild)
        enable_endpoint(bot, monkeypatch)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "link github", interaction, method="web")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "github.web.not_configured"


async def test_web_method_explains_when_the_endpoint_is_off(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        configure_web_flow(bot)
        guild = build_guild()
        interaction = make_interaction(guild)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "link github", interaction, method="web")
    finally:
        await bot.db.close()

    assert excinfo.value.key == "github.web.endpoint_off"


async def test_auto_falls_back_to_the_device_flow(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """没配齐（或端点没起）时，auto 不该报错，而该退回设备流。"""
    from bot.integrations.github import client as client_module

    monkeypatch.setattr(client_module, "MIN_INTERVAL_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        bot.settings.github_oauth_client_id = CLIENT_ID
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = FakeTransport(
            device={
                "device_code": "d",
                "user_code": "WXYZ-9999",
                "verification_uri": "https://github.com/login/device",
            },
            polls=[],
        )

        await run_command(bot, "link github", interaction, method="auto")

        assert "WXYZ-9999" in last_message(interaction).description
        await drain(cog)
    finally:
        await bot.db.close()


async def test_explicit_device_method_ignores_a_configured_web_flow(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    from bot.integrations.github import client as client_module

    monkeypatch.setattr(client_module, "MIN_INTERVAL_SECONDS", 0)
    bot = await setup_bot(settings)
    try:
        configure_web_flow(bot)
        enable_endpoint(bot, monkeypatch)
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = FakeTransport(
            device={
                "device_code": "d",
                "user_code": "WXYZ-9999",
                "verification_uri": "https://github.com/login/device",
            },
            polls=[],
        )

        await run_command(bot, "link github", interaction, method="device")

        assert "WXYZ-9999" in last_message(interaction).description
        await drain(cog)
    finally:
        await bot.db.close()


async def test_web_method_replies_with_an_authorize_button(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot = await setup_bot(settings)
    try:
        configure_web_flow(bot)
        enable_endpoint(bot, monkeypatch)
        guild = build_guild()
        interaction = make_interaction(guild)
        cog = bot.cogs["GitHubBridgeCog"]
        cog._transport = FakeTransport()

        await run_command(bot, "link github", interaction, method="web")

        sent = interaction.response._messages[-1]
        assert "github.com/login/oauth/authorize" in sent["embed"].description
        view = sent["view"]
        button = next(item for item in view.children if isinstance(item, discord.ui.Button))
        assert button.url.startswith("https://github.com/login/oauth/authorize?")
        assert len(cog._states) == 1
    finally:
        await bot.db.close()


async def test_the_callback_route_follows_the_redirect_uri(settings) -> None:
    """路由路径由回调地址推导，少一个会写错的配置项。"""
    settings.github_oauth_redirect_uri = REDIRECT_URI
    bot = await setup_bot(settings)
    try:
        assert ("GET", CALLBACK_PATH) in bot.web.routes
    finally:
        await bot.db.close()


async def test_no_callback_route_without_a_redirect_uri(settings) -> None:
    bot = await setup_bot(settings)
    try:
        assert bot.web.routes == ()
    finally:
        await bot.db.close()


async def drain(cog: Any) -> None:
    import asyncio

    for _ in range(50):
        tasks = list(getattr(cog, "_tasks", ()))
        if not tasks:
            return
        await asyncio.gather(*tasks, return_exceptions=True)
    raise AssertionError("后台任务没有结束")


# ---------------------------------------------------------------- 回调


CHINESE_BROWSER = {"Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8"}


async def call_callback(
    bot: Any,
    *,
    language: dict[str, str] = CHINESE_BROWSER,
    **params: Any,
) -> tuple[int, str]:
    """拿真 aiohttp 测试客户端打一次回调。

    默认带中文浏览器的 ``Accept-Language``：页面语言就是按它选的（Discord 的客户端语言
    在浏览器场景里拿不到）。要验英文回退就传 ``language={}``。
    """
    async with TestClient(TestServer(bot.web.build_app())) as client:
        response = await client.get(CALLBACK_PATH, params=params, headers=language)
        return response.status, await response.text()


async def prepare_callback(settings, monkeypatch: pytest.MonkeyPatch, *, transport: FakeTransport | None = None):
    """回调相关的用例都要在**建 bot 之前**把配置填好。

    回调路由是在 Cog 构造时按**构造时**的回调地址注册的（`test_the_callback_route_follows_the_redirect_uri`
    盯的就是这件事），所以事后改配置不会把路由补上。
    """
    settings.github_oauth_client_id = CLIENT_ID
    settings.github_oauth_client_secret = CLIENT_SECRET
    settings.github_oauth_redirect_uri = REDIRECT_URI
    bot = await setup_bot(settings)
    guild = build_guild()
    patch_guild(bot, guild, monkeypatch)
    cog = bot.cogs["GitHubBridgeCog"]
    cog._transport = transport or FakeTransport(polls=[{"access_token": "tok-1"}])
    return bot, guild, cog


async def test_callback_binds_the_account_and_tells_the_user(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, cog = await prepare_callback(settings, monkeypatch)
    try:
        pending = cog._states.create(guild_id=GUILD_ID, discord_user_id=MODERATOR_ID)

        status, html = await call_callback(bot, state=pending.state, code="code-1")

        account = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert status == 200
    assert GITHUB_LOGIN in html
    assert account is not None and account.github_login == GITHUB_LOGIN
    # 回调来自浏览器，手上没有交互对象，所以只能私信
    assert GITHUB_LOGIN in guild.get_member(MODERATOR_ID)._dms[-1]
    assert len(cog._states) == 0, "state 必须一次性用掉"


async def test_callback_rejects_an_unknown_state(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _guild, _cog = await prepare_callback(settings, monkeypatch)
    try:
        status, html = await call_callback(bot, state="made-up", code="code-1")

        account = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert status == 400
    assert "链接无效" in html
    assert account is None


async def test_callback_treats_a_reused_state_as_invalid(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _guild, cog = await prepare_callback(settings, monkeypatch)
    try:
        pending = cog._states.create(guild_id=GUILD_ID, discord_user_id=MODERATOR_ID)
        await call_callback(bot, state=pending.state, code="code-1")

        status, _html = await call_callback(bot, state=pending.state, code="code-2")
    finally:
        await bot.db.close()

    assert status == 400


async def test_callback_handles_a_denial(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, cog = await prepare_callback(settings, monkeypatch)
    try:
        pending = cog._states.create(guild_id=GUILD_ID, discord_user_id=MODERATOR_ID)

        status, html = await call_callback(bot, state=pending.state, error="access_denied")
    finally:
        await bot.db.close()

    assert status == 200
    assert "取消" in html
    assert "access_denied" in guild.get_member(MODERATOR_ID)._dms[-1]


async def test_callback_reports_a_taken_account(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, cog = await prepare_callback(settings, monkeypatch)
    try:
        await GitHubAccountStore(bot.db).link(
            GUILD_ID, discord_user_id=TARGET_ID, github_user_id=GITHUB_ID, github_login=GITHUB_LOGIN
        )
        pending = cog._states.create(guild_id=GUILD_ID, discord_user_id=MODERATOR_ID)

        status, html = await call_callback(bot, state=pending.state, code="code-1")

        mine = await GitHubAccountStore(bot.db).get(GUILD_ID, MODERATOR_ID)
    finally:
        await bot.db.close()

    assert status == 200
    assert "已经绑在另一个" in html
    assert mine is None
    assert f"<@{TARGET_ID}>" in guild.get_member(MODERATOR_ID)._dms[-1]


async def test_callback_reports_a_failed_exchange(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, guild, cog = await prepare_callback(
        settings,
        monkeypatch,
        transport=FakeTransport(polls=[{"error": "bad_verification_code"}]),
    )
    try:
        pending = cog._states.create(guild_id=GUILD_ID, discord_user_id=MODERATOR_ID)

        status, html = await call_callback(bot, state=pending.state, code="code-1")
    finally:
        await bot.db.close()

    assert status == 200
    assert "bad_verification_code" in guild.get_member(MODERATOR_ID)._dms[-1]
    assert "GitHub 拒绝了" in html or "refused" in html


async def test_callback_without_a_code_is_invalid(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _guild, cog = await prepare_callback(settings, monkeypatch)
    try:
        pending = cog._states.create(guild_id=GUILD_ID, discord_user_id=MODERATOR_ID)

        status, _html = await call_callback(bot, state=pending.state)
    finally:
        await bot.db.close()

    assert status == 400


async def test_the_page_falls_back_to_the_default_locale(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    """浏览器没说自己要什么语言时，用默认语言，而不是拼一个乱七八糟的猜测。"""
    bot, _guild, _cog = await prepare_callback(settings, monkeypatch)
    try:
        status, html = await call_callback(bot, language={}, state="made-up", code="code-1")
    finally:
        await bot.db.close()

    assert status == 400
    assert "Link is not valid" in html


async def test_the_page_follows_the_browser_language(settings, monkeypatch: pytest.MonkeyPatch) -> None:
    bot, _guild, _cog = await prepare_callback(settings, monkeypatch)
    try:
        status, html = await call_callback(
            bot, language={"Accept-Language": "fr-FR,nb;q=0.9"}, state="made-up", code="code-1"
        )
    finally:
        await bot.db.close()

    assert status == 400
    # 我们没有法语文案 → 回退默认语言，而不是把不认识的 tag 当语言用
    assert "Link is not valid" in html
