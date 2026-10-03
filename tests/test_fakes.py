"""替身纪律：假对象不许「发明」真实 discord.py 类上不存在的属性。

假成员若自己实现一个真实 ``Member`` 没有的同名方法，调用点抛出的 ``AttributeError``
就会被测试掩护过去 —— 命令能注册、测试全绿，真机上一调就崩。
只要假对象只能提供真 API，这类错误就会在测试阶段暴露。
"""

from __future__ import annotations

from typing import Any

import discord
import pytest

from tests.discord_fakes import (
    MESSAGE_ID,
    MODERATOR_ID,
    FakeCategory,
    FakeChannel,
    FakeInteraction,
    FakeMember,
    FakeMessage,
    FakePayload,
    FakeResponse,
    FakeRole,
    FakeThread,
    build_guild,
)


def samples() -> list[tuple[str, Any, Any]]:
    guild = build_guild()
    user = guild.get_member(MODERATOR_ID)
    channel = guild.channels[0]
    return [
        ("member", FakeMember(1, 5), discord.Member),
        ("role", FakeRole(1, 5), discord.Role),
        ("response", FakeResponse(), discord.InteractionResponse),
        ("guild", guild, discord.Guild),
        ("interaction", FakeInteraction(user=user, guild=guild), discord.Interaction),
        ("channel", FakeChannel(1), discord.TextChannel),
        ("thread", FakeThread(1), discord.Thread),
        ("category", FakeCategory(1), discord.CategoryChannel),
        ("message", FakeMessage(1, channel), discord.Message),
        ("payload", FakePayload(user_id=1, member=user, emoji="👍"), discord.RawReactionActionEvent),
    ]


@pytest.mark.parametrize(("label", "fake", "real"), samples(), ids=[sample[0] for sample in samples()])
def test_fake_only_exposes_real_api(label: str, fake: Any, real: type) -> None:
    declared = set(vars(type(fake))) | set(vars(fake))
    invented = sorted(name for name in declared if not name.startswith("_") and not hasattr(real, name))

    assert invented == [], f"{label} 用的假对象发明了 {real.__name__} 上不存在的属性：{invented}"


def test_member_fake_does_not_have_is_default() -> None:
    """``is_default()`` 属于 Role 而不属于 Member，单独钉一条。"""
    assert not hasattr(discord.Member, "is_default")
    assert not hasattr(FakeMember(1, 5), "is_default")
    assert hasattr(discord.Role, "is_default")


def test_build_guild_wires_roles_the_way_discord_does() -> None:
    guild = build_guild()

    assert guild.default_role.id == guild.id, "@everyone 的 id 就是 guild id"
    assert guild.default_role.is_default()
    for member in guild.members:
        assert member.roles[0] is guild.default_role
        assert member.top_role is max(member.roles, key=lambda role: role.position)
        assert member in member.top_role.members


def test_fake_reaction_payload_uses_the_same_emoji_form_as_the_db() -> None:
    """存库和事件比对必须用同一种表情写法，否则反应永远匹配不上。"""
    unicode_payload = FakePayload(user_id=1, member=None, emoji="👍")
    custom_payload = FakePayload(user_id=1, member=None, emoji="<:party:123>")

    assert str(unicode_payload.emoji) == "👍"
    assert str(custom_payload.emoji) == "<:party:123>"
    assert str(discord.PartialEmoji.from_str("👍")) == "👍"
    assert str(discord.PartialEmoji.from_str("<:party:123>")) == "<:party:123>"
    assert unicode_payload.message_id == MESSAGE_ID
