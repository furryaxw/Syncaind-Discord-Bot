"""节点 → 角色同步。

两条安全边界各有一组用例盯着（这是这个模块存在的全部理由）：

1. **只动绑定表里出现过的角色** —— 别人手动发的角色永远不会被同步撤掉；
2. **读失败绝不等于「他没有权限」** —— 读不到就跳过这个人，不能按空集合去撤他的角色，
   否则一次网络抖动会把所有人的角色撤光。
"""

from __future__ import annotations

from typing import Any

import pytest

from bot.core.errors import UserError
from bot.integrations.github import GitHubAccountStore
from bot.integrations.smas import AccessSourceError, NodeRoleBindingStore
from tests.discord_fakes import (
    GUILD_ID,
    MODERATOR_ID,
    TARGET_ID,
    FakeInteraction,
    FakeRole,
    build_guild,
    run_command,
    setup_bot,
)
from tests.smas_fakes import FakeAccessSource

NODE = "team.acme.packages.read"
PATTERN = "team.acme.*"
# 替身里成员的 top_role id 是 member_id * 10，所以别用那种形状的编号，免得撞上。
SPARE_ROLE_ID = 900_001
COSMETIC_ROLE_ID = 900_002


def spare_role(guild: Any, role_id: int = SPARE_ROLE_ID) -> FakeRole:
    """造一个**谁都没有**的角色当同步目标（位置 20，低于机器人）。

    不能用某个成员的 top_role：那个人本来就有它，同步会正确地把它撤掉 ——
    测「加角色」时那是个假失败。
    """
    role = FakeRole(role_id, 20, name=f"role-{role_id}")
    guild.roles.append(role)
    return role


def interaction_for(guild: Any, member: Any = None) -> FakeInteraction:
    return FakeInteraction(user=member or guild.get_member(MODERATOR_ID), guild=guild)


async def link(bot: Any, discord_user_id: int, github_user_id: int) -> None:
    await GitHubAccountStore(bot.db).link(
        GUILD_ID,
        discord_user_id=discord_user_id,
        github_user_id=github_user_id,
        github_login=f"user{github_user_id}",
    )


async def prepare(
    settings: Any,
    *,
    nodes: dict[str, set[str]] | None = None,
    errors: dict[str, Exception] | None = None,
    bind_pattern: str = PATTERN,
) -> tuple[Any, Any, Any, Any, Any]:
    """常见装配：一个 guild、两个绑了 GitHub 的成员、一条绑定、一个假来源。"""
    bot = await setup_bot(settings)
    guild = build_guild()
    await link(bot, MODERATOR_ID, 100)
    await link(bot, TARGET_ID, 200)
    role = spare_role(guild)
    store = NodeRoleBindingStore(bot.db)
    await store.bind(GUILD_ID, pattern=bind_pattern, role_id=role.id, created_by=MODERATOR_ID)
    source = FakeAccessSource(nodes, errors=errors)
    cog = bot.cogs["AccessRolesCog"]
    cog._source = source
    return bot, guild, role, source, cog


# ---------------------------------------------------------------- 同步：加/撤


async def test_sync_grants_the_role_for_a_held_node(settings: Any) -> None:
    bot, guild, role, source, cog = await prepare(settings, nodes={"200": {NODE}})
    try:
        report = await cog.sync_guild(guild)

        target = guild.get_member(TARGET_ID)
    finally:
        await bot.db.close()

    assert report.checked == 2 and report.added == 1 and report.removed == 0
    assert f"add_role:{role.id}" in target._calls
    assert source.calls == ["100", "200"], "来源要按 GitHub 数字 id 查（那张映射表就是桥梁）"


async def test_sync_revokes_when_the_node_is_gone(settings: Any) -> None:
    bot, guild, role, _source, cog = await prepare(settings, nodes={})
    try:
        target = guild.get_member(TARGET_ID)
        await target.add_roles(role)
        target._calls.clear()

        report = await cog.sync_guild(guild)
    finally:
        await bot.db.close()

    assert report.removed == 1
    assert f"remove_role:{role.id}" in target._calls


async def test_sync_never_touches_unbound_roles(settings: Any) -> None:
    """**边界一**：没绑过的角色（比如手动发的）永远不该被同步碰掉。"""
    bot, guild, role, _source, cog = await prepare(settings, nodes={})
    try:
        target = guild.get_member(TARGET_ID)
        cosmetic = spare_role(guild, COSMETIC_ROLE_ID)
        # 同时给他「绑过的」和「没绑过的」：前者该被撤，后者一个字都不该动。
        target.roles = [*target.roles, role, cosmetic]
        target._calls.clear()

        await cog.sync_guild(guild)
    finally:
        await bot.db.close()

    assert not any(f"remove_role:{cosmetic.id}" in call for call in target._calls)
    assert f"remove_role:{role.id}" in target._calls, "绑过的那条仍然照常撤"


async def test_a_read_failure_never_revokes(settings: Any) -> None:
    """**边界二**：读不到权限的人要跳过，不能当成「他没有权限」。"""
    bot, guild, role, _source, cog = await prepare(settings, errors={"200": AccessSourceError("boom")})
    try:
        target = guild.get_member(TARGET_ID)
        await target.add_roles(role)
        target._calls.clear()

        report = await cog.sync_guild(guild)
    finally:
        await bot.db.close()

    assert report.failed == 1
    assert report.removed == 0
    assert target._calls == [], "读失败时一个字都不该动"


async def test_dry_run_changes_nothing(settings: Any) -> None:
    bot, guild, _role, _source, cog = await prepare(settings, nodes={"200": {NODE}})
    try:
        report = await cog.sync_guild(guild, dry_run=True)

        target = guild.get_member(TARGET_ID)
    finally:
        await bot.db.close()

    assert report.added == 1, "预览要说清会改什么"
    assert target._calls == [], "但一个角色都不许动"


async def test_a_role_above_the_bot_is_skipped(settings: Any) -> None:
    bot, guild, _role, _source, cog = await prepare(settings, nodes={"200": {NODE}})
    try:
        # 机器人自己的最高角色：它管不了（位置相同也视为不可管理），所以拿去当「层级不够」的例子。
        too_high = guild.me.top_role
        await NodeRoleBindingStore(bot.db).bind(
            GUILD_ID, pattern="system.users.read", role_id=too_high.id, created_by=MODERATOR_ID
        )
        cog._source = FakeAccessSource({"200": {NODE, "system.users.read"}})

        report = await cog.sync_guild(guild)

        target = guild.get_member(TARGET_ID)
    finally:
        await bot.db.close()

    assert report.skipped >= 1
    assert not any(f"add_role:{too_high.id}" in call for call in target._calls)


async def test_an_account_without_a_member_is_skipped(settings: Any) -> None:
    bot, guild, _role, _source, cog = await prepare(settings, nodes={"200": {NODE}})
    try:
        # 再绑一个不在服务器里的 Discord 用户
        await link(bot, 999_999, 300)
        report = await cog.sync_guild(guild)
    finally:
        await bot.db.close()

    assert report.skipped == 1
    assert report.checked == 2


# ---------------------------------------------------------------- 命令


@pytest.mark.parametrize(
    ("path", "params"),
    [
        ("access bind", {"node": NODE, "role": None}),
        ("access unbind", {"node": PATTERN}),
        ("access list", {}),
        ("access sync", {}),
    ],
)
async def test_smas_commands_require_a_linked_github_account(settings: Any, path: str, params: dict[str, Any]) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        if params.get("role") is None and "role" in params:
            params["role"] = guild.get_member(TARGET_ID).top_role

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, path, interaction_for(guild), **params)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "github.link_required"


async def test_bind_list_and_unbind(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link(bot, MODERATOR_ID, 100)
        role = guild.get_member(TARGET_ID).top_role

        await run_command(bot, "access bind", interaction_for(guild), node=PATTERN, role=role)
        listed = interaction_for(guild)
        await run_command(bot, "access list", listed)
        removed = interaction_for(guild)
        await run_command(bot, "access unbind", removed, node=PATTERN)

        remaining = await NodeRoleBindingStore(bot.db).all(GUILD_ID)
    finally:
        await bot.db.close()

    assert PATTERN in listed.response._messages[-1]["embed"].description
    assert remaining == []
    assert "已删除" in removed.response._messages[-1]["embed"].description


async def test_a_bad_pattern_is_explained(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link(bot, MODERATOR_ID, 100)
        role = guild.get_member(TARGET_ID).top_role

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "access bind", interaction_for(guild), node="team.*.read", role=role)
    finally:
        await bot.db.close()

    assert excinfo.value.key == "access.bad_node"


async def test_sync_without_configuration_says_what_to_set(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        guild = build_guild()
        await link(bot, MODERATOR_ID, 100)

        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "access sync", interaction_for(guild))
    finally:
        await bot.db.close()

    assert excinfo.value.key == "smas.not_configured"


async def test_status_shows_nodes_and_the_roles_they_imply(settings: Any) -> None:
    bot, guild, role, _source, _cog = await prepare(settings, nodes={"200": {NODE}})
    try:
        interaction = interaction_for(guild)
        await run_command(bot, "access status", interaction, member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    description = interaction.response._edits[-1]["embed"].description
    assert NODE in description
    assert f"<@&{role.id}>" in description


async def test_status_reports_a_read_failure_instead_of_pretending(settings: Any) -> None:
    bot, guild, _role, _source, _cog = await prepare(settings, errors={"200": AccessSourceError("boom")})
    try:
        with pytest.raises(UserError) as excinfo:
            await run_command(bot, "access status", interaction_for(guild), member=guild.get_member(TARGET_ID))
    finally:
        await bot.db.close()

    assert excinfo.value.key == "access.status.read_failed"


async def test_the_module_registers_its_commands(settings: Any) -> None:
    bot = await setup_bot(settings)
    try:
        group = next(command for command in bot.tree.get_commands() if command.name == "access")
        names = {child.name for child in group.commands}  # type: ignore[attr-defined]
    finally:
        await bot.db.close()

    assert names == {"bind", "unbind", "list", "sync", "status"}
