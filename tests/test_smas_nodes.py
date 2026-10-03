"""权限节点匹配与「该有哪些角色」的判定 —— 纯函数，和 Discord 无关。

这块逻辑决定谁被加角色、谁被撤角色，所以逐条钉：通配边界、大小写、空输入、
以及最重要的一条 —— **只返回绑定表里出现过的角色**。
"""

from __future__ import annotations

import pytest

from bot.integrations.smas import (
    NodePatternError,
    NodeRoleBindingStore,
    desired_role_ids,
    is_valid_pattern,
    matches,
    normalize_pattern,
)
from tests.discord_fakes import GUILD_ID, MODERATOR_ID

# ---------------------------------------------------------------- 模式校验


@pytest.mark.parametrize(
    "pattern",
    [
        "system.users.read",
        "team.acme.packages.read",
        "team.acme.*",
        "team.*",
        "system-teams.read",
        "team.acme.0_9-x.read",
        "Team.Acme.Read",  # 大小写不敏感：会被规范成小写
    ],
)
def test_valid_patterns(pattern: str) -> None:
    assert is_valid_pattern(pattern)


@pytest.mark.parametrize(
    "pattern",
    [
        "",
        "   ",
        "team..read",
        "team.*.read",  # 中间不允许通配（服务端也没有这个能力）
        "team.*.*",
        "team.acme.**",
        "team.acme.re ad",
        ".system",
    ],
)
def test_invalid_patterns(pattern: str) -> None:
    assert not is_valid_pattern(pattern)


def test_normalize_lowercases() -> None:
    assert normalize_pattern(" Team.Acme.* ") == "team.acme.*"


def test_normalize_raises_with_a_readable_reason() -> None:
    with pytest.raises(NodePatternError) as excinfo:
        normalize_pattern("team.*.read")

    assert "末段" in str(excinfo.value)


# ---------------------------------------------------------------- 匹配


def test_exact_match() -> None:
    assert matches("system.users.read", "system.users.read")
    assert not matches("system.users.read", "system.users.manage")


def test_wildcard_covers_the_whole_subtree() -> None:
    assert matches("team.acme.*", "team.acme.packages.read")
    assert matches("team.acme.*", "team.acme.example_mod.manage")


def test_wildcard_does_not_match_the_prefix_itself() -> None:
    """`team.acme.*` 不该匹配 `team.acme` —— 那是「Team 本身」，不是它下面的节点。"""
    assert not matches("team.acme.*", "team.acme")


def test_a_wildcard_team_matches_any_team() -> None:
    assert matches("team.*", "team.other.packages.read")
    assert matches("team.*", "team.acme.keys.distribute")


def test_matching_is_case_insensitive() -> None:
    assert matches("Team.Acme.*", "team.acme.packages.read")


def test_empty_inputs_never_match() -> None:
    assert not matches("", "system.users.read")
    assert not matches("system.users.read", "")


def test_unrelated_patterns_do_not_match() -> None:
    assert not matches("team.acme.*", "team.beta.packages.read")
    assert not matches("system.*", "team.acme.packages.read")


# ---------------------------------------------------------------- 该有哪些角色


def test_only_roles_from_the_binding_table() -> None:
    """最重要的一条安全边界：判定结果里绝不会出现没绑过的角色。"""
    bindings = [("team.acme.packages.read", 111)]

    assert desired_role_ids(["team.acme.packages.read"], bindings) == {111}
    assert desired_role_ids(["system.users.manage"], bindings) == set()


def test_several_patterns_union() -> None:
    bindings = [("team.acme.*", 111), ("system.users.read", 222)]

    assert desired_role_ids(["team.acme.packages.read", "system.users.read"], bindings) == {111, 222}


def test_one_node_can_map_to_several_roles() -> None:
    bindings = [("team.acme.*", 111), ("team.acme.*", 222)]

    assert desired_role_ids(["team.acme.packages.read"], bindings) == {111, 222}


def test_no_nodes_means_no_roles() -> None:
    assert desired_role_ids([], [("team.acme.*", 111)]) == set()


# ---------------------------------------------------------------- 存储


async def test_bind_and_unbind(db) -> None:
    store = NodeRoleBindingStore(db)

    assert await store.bind(GUILD_ID, pattern="team.acme.*", role_id=111, created_by=MODERATOR_ID) is True
    assert await store.bind(GUILD_ID, pattern="team.acme.*", role_id=111, created_by=MODERATOR_ID) is False

    assert await store.pairs(GUILD_ID) == [("team.acme.*", 111)]
    assert await store.bound_role_ids(GUILD_ID) == {111}

    assert await store.unbind(GUILD_ID, pattern="team.acme.*", role_id=111) == 1
    assert await store.all(GUILD_ID) == []


async def test_unbind_without_a_role_removes_them_all(db) -> None:
    store = NodeRoleBindingStore(db)
    await store.bind(GUILD_ID, pattern="team.acme.*", role_id=111, created_by=MODERATOR_ID)
    await store.bind(GUILD_ID, pattern="team.acme.*", role_id=222, created_by=MODERATOR_ID)

    assert await store.unbind(GUILD_ID, pattern="team.acme.*") == 2
    assert await store.all(GUILD_ID) == []


async def test_bind_normalises_the_pattern(db) -> None:
    store = NodeRoleBindingStore(db)

    await store.bind(GUILD_ID, pattern="Team.Acme.*", role_id=111, created_by=MODERATOR_ID)

    assert await store.pairs(GUILD_ID) == [("team.acme.*", 111)]


async def test_bind_rejects_a_bad_pattern(db) -> None:
    store = NodeRoleBindingStore(db)

    with pytest.raises(NodePatternError):
        await store.bind(GUILD_ID, pattern="team.*.read", role_id=111, created_by=MODERATOR_ID)


async def test_bound_role_ids_is_the_allowlist(db) -> None:
    """同步时只允许动这些角色 —— 其余角色即使成员有，也不该被碰。"""
    store = NodeRoleBindingStore(db)
    await store.bind(GUILD_ID, pattern="team.acme.*", role_id=111, created_by=MODERATOR_ID)
    await store.bind(GUILD_ID, pattern="system.users.read", role_id=222, created_by=MODERATOR_ID)

    assert await store.bound_role_ids(GUILD_ID) == {111, 222}
