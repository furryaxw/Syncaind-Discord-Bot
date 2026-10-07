"""测试替身：够用的假对象，避免为了跑测试去连 Discord。

**纪律：这些假对象只允许出现真实 discord.py 类上存在的属性。**

假成员若自己「发明」一个真实 ``Member`` 没有的同名方法（例如 ``is_default()``，
它其实是 ``Role`` 的方法），调用点抛出的 ``AttributeError`` 就会被单元测试掩护过去 ——
测试通过，真机一跑就炸。``tests/test_fakes.py`` 会逐类核对这条纪律。

测试专用的「记录」与「注入」一律用下划线开头的属性，这样它们不会污染上面那条检查。
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from typing import Any

import discord

from bot.core.bot import SyncaindBot
from tests.conftest import GUILD_ID

MODERATOR_ID = 400
TARGET_ID = 300
BOT_ID = 500
MOD_LOG_CHANNEL_ID = 555
MESSAGE_ID = 8888

MADE_UP_TIME = datetime(2026, 1, 1, tzinfo=timezone.utc)


class FakeResponse:
    """对应 :class:`discord.InteractionResponse`。"""

    def __init__(self) -> None:
        self._messages: list[dict[str, Any]] = []
        self._edits: list[dict[str, Any]] = []
        self._deferred = False
        # 注入用：让 defer 抛（例如 Discord 的 10062「交互已过期」）。
        self._defer_error: Exception | None = None

    def is_done(self) -> bool:
        return self._deferred

    async def send_message(self, content: Any = None, **kwargs: Any) -> None:
        """真实签名是 ``send_message(content, *, ...)`` —— content 可以按位置传。"""
        if content is not None:
            kwargs["content"] = content
        self._messages.append(kwargs)

    async def defer(self, **kwargs: Any) -> None:
        if self._defer_error is not None:
            raise self._defer_error
        self._deferred = True


class FakeFollowup:
    """对应 :class:`discord.Interaction.followup`（真实类型是 ``discord.Webhook``）。"""

    def __init__(self) -> None:
        self._sent: list[dict[str, Any]] = []
        # 注入用：让 followup 也失败（交互过期时后续消息同样发不出去）。
        self._error: Exception | None = None

    async def send(self, content: Any = None, **kwargs: Any) -> None:
        """真实签名是 ``send(content, *, ...)`` —— content 可以按位置传。"""
        if self._error is not None:
            raise self._error
        if content is not None:
            kwargs["content"] = content
        self._sent.append(kwargs)


class FakeRole:
    """对应 :class:`discord.Role`。"""

    def __init__(
        self,
        role_id: int,
        position: int,
        *,
        name: str = "role",
        managed: bool = False,
        default: bool = False,
        permissions: discord.Permissions | None = None,
        hoist: bool = False,
        mentionable: bool = False,
        colour: discord.Colour | None = None,
    ) -> None:
        self.id = role_id
        self.name = name
        self.mention = f"<@&{role_id}>"
        self.position = position
        self.managed = managed
        self.permissions = permissions if permissions is not None else discord.Permissions.none()
        self.hoist = hoist
        self.mentionable = mentionable
        self.colour = colour if colour is not None else discord.Colour.default()
        self.members: list[FakeMember] = []
        self._default = default
        self._edits: list[dict[str, Any]] = []
        self._deleted = False

    def is_default(self) -> bool:
        return self._default

    async def edit(self, **kwargs: Any) -> None:
        self._edits.append(kwargs)
        for key, value in kwargs.items():
            if key == "colour":
                self.colour = value
            elif key in {"name", "hoist", "mentionable", "permissions"}:
                setattr(self, key, value)

    async def delete(self, **kwargs: Any) -> None:
        self._deleted = True


class FakeMember:
    """对应 :class:`discord.Member`。处罚与角色变更都记在 ``_calls`` 里。

    属性清单要覆盖 ``discord.abc.User`` 这个 Protocol 的全部成员，
    这样 ``isinstance(fake, discord.abc.User)`` 才会成立——生产代码里有这个判断，
    假对象不满足的话，那条分支就永远测不到。
    """

    def __init__(
        self,
        member_id: int,
        position: int,
        *,
        name: str = "member",
        bot: bool = False,
        guild: Any = None,
        permissions: discord.Permissions | None = None,
    ) -> None:
        self.id = member_id
        self.name = name
        self.display_name = name
        self.global_name = name
        self.discriminator = "0"
        self.mention = f"<@{member_id}>"
        self.bot = bot
        self.system = False
        self.nick = None
        self.guild = guild
        self.top_role = FakeRole(role_id=member_id * 10, position=position, name=f"{name}-role", default=position == 0)
        self.roles: list[FakeRole] = [self.top_role]
        self.guild_permissions = permissions if permissions is not None else discord.Permissions.none()
        self.avatar = None
        self.default_avatar = SimpleNamespace(url="https://example.invalid/default.png")
        self.display_avatar = SimpleNamespace(url="https://example.invalid/avatar.png")
        self.avatar_decoration = None
        self.avatar_decoration_sku_id = None
        self.joined_at = MADE_UP_TIME
        self.created_at = MADE_UP_TIME
        self.timed_out_until = None
        self._calls: list[str] = []
        self._dms: list[str] = []
        self._fail_dm = False
        self._timeout_for: Any = None

    def mentioned_in(self, message: Any) -> bool:
        return self in getattr(message, "mentions", [])

    def _refresh_top_role(self) -> None:
        self.top_role = max(self.roles, key=lambda role: role.position)

    async def kick(self, *, reason: str | None = None) -> None:
        self._calls.append("kick")

    async def ban(self, *, reason: str | None = None, delete_message_seconds: int = 0) -> None:
        self._calls.append("ban")

    async def timeout(self, duration: Any, *, reason: str | None = None) -> None:
        self._calls.append("timeout")
        self._timeout_for = duration

    async def add_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        for role in roles:
            self._calls.append(f"add_role:{role.id}")
            if role not in self.roles:
                self.roles.append(role)
                role.members.append(self)
        self._refresh_top_role()

    async def remove_roles(self, *roles: FakeRole, reason: str | None = None) -> None:
        for role in roles:
            self._calls.append(f"remove_role:{role.id}")
            if role in self.roles:
                self.roles.remove(role)
            if self in role.members:
                role.members.remove(self)
        self._refresh_top_role()

    async def send(self, content: Any = None, **kwargs: Any) -> None:
        if self._fail_dm:
            # 真实场景：对方关了「允许来自服务器成员的私信」→ 403
            raise discord.Forbidden(
                SimpleNamespace(status=403, reason="Forbidden"),
                "Cannot send messages to this user",
            )
        self._calls.append("dm")
        self._dms.append(str(content if content is not None else kwargs.get("embed", "")))


class FakeMessage:
    """对应 :class:`discord.Message`（只实现断言里会碰到的那部分）。"""

    def __init__(self, message_id: int, channel: Any, *, content: str = "hello") -> None:
        self.id = message_id
        self.channel = channel
        self.content = content
        self.author = None
        self.created_at = MADE_UP_TIME
        self.mentions: list[Any] = []
        self.embeds: list[Any] = []
        self._edits: list[dict[str, Any]] = []

    @property
    def jump_url(self) -> str:
        return f"https://discord.com/invite/{self.id}"

    async def edit(self, **kwargs: Any) -> None:
        self._edits.append(kwargs)
        if "embed" in kwargs:
            self.embeds = [kwargs["embed"]] if kwargs["embed"] is not None else []


class FakeChannel(discord.abc.Messageable):
    """对应 :class:`discord.TextChannel`（只实现会用到的那部分）。"""

    def __init__(self, channel_id: int, *, name: str = "general") -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.overwrites: dict[Any, discord.PermissionOverwrite] = {}
        self._sent: list[dict[str, Any]] = []
        self._edits: list[dict[str, Any]] = []
        self._permissions: list[dict[str, Any]] = []
        self._deleted = False
        self._messages: dict[int, FakeMessage] = {}
        self._next_message_id = 900_000

    async def _get_channel(self) -> FakeChannel:
        return self

    async def send(self, **kwargs: Any) -> FakeMessage:
        """真实 ``channel.send`` 会返回刚发出的 Message —— 按钮/后续编辑都靠它拿 id。"""
        self._sent.append(kwargs)
        message = FakeMessage(self._next_message_id, self)
        self._next_message_id += 1
        message.embeds = [kwargs["embed"]] if kwargs.get("embed") is not None else []
        self._messages[message.id] = message
        return message

    async def edit(self, **kwargs: Any) -> None:
        self._edits.append(kwargs)
        if "name" in kwargs:
            self.name = kwargs["name"]

    async def delete(self, **kwargs: Any) -> None:
        self._deleted = True

    async def set_permissions(self, target: Any, *, overwrite: Any = None, **kwargs: Any) -> None:
        self._permissions.append({"target": target, "overwrite": overwrite, **kwargs})

    async def fetch_message(self, message_id: int) -> FakeMessage:
        message = self._messages.get(message_id)
        if message is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "Unknown Message"})
        return message


class FakeThread(discord.abc.Messageable):
    """对应 :class:`discord.Thread`——帖子和频道一样能发消息，但**没有**频道管理那一套。

    特意不继承 ``FakeChannel``：真实 ``Thread`` 上并没有 ``overwrites`` / ``set_permissions``，
    继承过来就等于替身替真实类「发明」了 API（``tests/test_fakes.py`` 会当场抓住）。
    """

    def __init__(self, channel_id: int, *, name: str = "a thread", parent: Any = None) -> None:
        self.id = channel_id
        self.name = name
        self.mention = f"<#{channel_id}>"
        self.parent = parent
        self.guild: Any = None
        self.archived = False
        self.locked = False
        self._sent: list[dict[str, Any]] = []
        self._messages: dict[int, FakeMessage] = {}
        self._next_message_id = 800_000

    async def _get_channel(self) -> FakeThread:
        return self

    async def send(self, **kwargs: Any) -> FakeMessage:
        self._sent.append(kwargs)
        message = FakeMessage(self._next_message_id, self)
        self._next_message_id += 1
        message.embeds = [kwargs["embed"]] if kwargs.get("embed") is not None else []
        self._messages[message.id] = message
        return message

    async def fetch_message(self, message_id: int) -> FakeMessage:
        message = self._messages.get(message_id)
        if message is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "Unknown Message"})
        return message


class FakeCategory:
    """对应 :class:`discord.CategoryChannel`。"""

    def __init__(self, category_id: int, *, name: str = "category") -> None:
        self.id = category_id
        self.name = name
        self.mention = f"<#{category_id}>"
        self.channels: list[FakeChannel] = []
        self.overwrites: dict[Any, discord.PermissionOverwrite] = {}
        self._deleted = False

    async def set_permissions(self, target: Any, *, overwrite: Any = None, **kwargs: Any) -> None:
        for channel in self.channels:
            await channel.set_permissions(target, overwrite=overwrite, **kwargs)

    async def edit(self, **kwargs: Any) -> None:
        if "name" in kwargs:
            self.name = kwargs["name"]

    async def delete(self, **kwargs: Any) -> None:
        self._deleted = True


class FakeGuild:
    """对应 :class:`discord.Guild`。"""

    def __init__(
        self,
        *,
        owner_id: int,
        members: list[FakeMember],
        channels: list[Any] | None = None,
        name: str = "Test Guild",
    ) -> None:
        self.id = GUILD_ID
        self.name = name
        self.owner_id = owner_id
        self.description = None
        self.member_count = len(members)
        self.created_at = MADE_UP_TIME
        self.verification_level = discord.VerificationLevel.medium
        self.premium_tier = 0
        self.premium_subscription_count = 0
        self.icon = None
        self.members = members
        self.me = next((member for member in members if member.bot), None)
        self.channels = list(channels or [])
        for member in members:
            member.guild = self

        # @everyone 角色的 id 就是 guild id，和真实情况一致。
        self.default_role = FakeRole(self.id, 0, name="@everyone", default=True)
        for member in members:
            own_top = member.top_role
            others = [role for role in member.roles if role is not own_top and role is not self.default_role]
            member.roles = [self.default_role, *others, own_top]
            member._refresh_top_role()
        self.roles: list[FakeRole] = [self.default_role]
        for member in members:
            for role in member.roles:
                if role not in self.roles:
                    self.roles.append(role)
                if member not in role.members:
                    role.members.append(member)

        self._channels = {channel.id: channel for channel in self.channels}
        for channel in self.channels:
            # 真实频道知道自己属于哪个 guild；推送时要按 guild 取语言。
            if getattr(channel, "guild", None) is None:
                channel.guild = self
        self._fetchable_members: dict[int, FakeMember] = {}
        self._banned: set[int] = set()
        self._unbans: list[dict[str, Any]] = []
        self._created_roles: list[FakeRole] = []
        self._created_channels: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ 查询

    def get_member(self, user_id: int) -> FakeMember | None:
        return next((member for member in self.members if member.id == user_id), None)

    def get_channel(self, channel_id: int) -> Any | None:
        return self._channels.get(channel_id)

    def get_channel_or_thread(self, channel_id: int) -> Any | None:
        """真实 ``Guild.get_channel`` **不返回帖子**，找帖子必须用这个方法。"""
        return self._channels.get(channel_id)

    def get_role(self, role_id: int) -> FakeRole | None:
        return next((role for role in self.roles if role.id == role_id), None)

    async def fetch_member(self, user_id: int) -> FakeMember:
        member = self.get_member(user_id) or self._fetchable_members.get(user_id)
        if member is None:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "Unknown Member"})
        return member

    async def fetch_ban(self, user: Any) -> None:
        """默认谁都不在封禁名单里；测试用 ``guild._banned.add(id)`` 制造「已封禁」。"""
        if getattr(user, "id", None) not in self._banned:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), {"message": "Unknown Ban"})
        return None

    # ------------------------------------------------------------------ 变更

    async def unban(self, user: Any, *, reason: str | None = None) -> None:
        self._unbans.append({"user": user, "reason": reason})

    async def create_role(self, name: str, **kwargs: Any) -> FakeRole:
        role = FakeRole(
            role_id=7000 + len(self._created_roles),
            position=max((role.position for role in self.roles), default=0) + 1,
            name=name,
            permissions=kwargs.get("permissions"),
            hoist=kwargs.get("hoist", False),
            mentionable=kwargs.get("mentionable", False),
            colour=kwargs.get("colour"),
        )
        self._created_roles.append(role)
        self.roles.append(role)
        return role

    async def create_text_channel(self, name: str, **kwargs: Any) -> FakeChannel:
        channel = FakeChannel(8000 + len(self.channels), name=name)
        self.channels.append(channel)
        self._channels[channel.id] = channel
        category = kwargs.get("category")
        if category is not None:
            category.channels.append(channel)
        self._created_channels.append({"kind": "text", "name": name, "kwargs": kwargs, "channel": channel})
        return channel

    async def create_voice_channel(self, name: str, **kwargs: Any) -> FakeChannel:
        channel = await self.create_text_channel(name, **kwargs)
        self._created_channels[-1]["kind"] = "voice"
        return channel

    async def create_category(self, name: str, **kwargs: Any) -> FakeCategory:
        category = FakeCategory(9000 + len(self.channels), name=name)
        self.channels.append(category)
        self._channels[category.id] = category
        self._created_channels.append({"kind": "category", "name": name, "kwargs": kwargs, "channel": category})
        return category


class FakePayload:
    """对应 :class:`discord.RawReactionActionEvent`。"""

    def __init__(
        self,
        *,
        user_id: int,
        member: Any,
        emoji: str,
        message_id: int = MESSAGE_ID,
        channel_id: int = MOD_LOG_CHANNEL_ID,
        guild_id: int = GUILD_ID,
    ) -> None:
        self.guild_id = guild_id
        self.message_id = message_id
        self.channel_id = channel_id
        self.user_id = user_id
        self.member = member
        self.emoji = discord.PartialEmoji.from_str(emoji)
        self.event_type = "REACTION_ADD"


class FakeInteraction:
    """对应 :class:`discord.Interaction`。"""

    def __init__(
        self,
        *,
        user: FakeMember,
        guild: FakeGuild,
        channel: Any = None,
        locale: str = "zh-CN",
        permissions: discord.Permissions | None = None,
        app_permissions: discord.Permissions | None = None,
        owner_id: int | None = None,
    ) -> None:
        self.user = user
        self.guild = guild
        self.channel = channel if channel is not None else (guild.channels[0] if guild.channels else None)
        self.locale = locale
        self.command = None
        # 真实 Interaction 有 message（按钮/组件交互时是那条消息）。
        self.message: Any = None
        # 真实 Interaction 有 created_at；命令完成日志会用它算「从交互创建到完成」的耗时。
        self.created_at = datetime.now(timezone.utc)
        self.response = FakeResponse()
        # 真实 Interaction 有 followup（defer 之后的回应都从这里走）。
        self.followup = FakeFollowup()
        self.client = SimpleNamespace(
            user=SimpleNamespace(id=BOT_ID),
            tree=None,
            settings=SimpleNamespace(owner_id=owner_id),
        )
        self.permissions = permissions if permissions is not None else discord.Permissions.all()
        self.app_permissions = app_permissions if app_permissions is not None else discord.Permissions.all()

    async def edit_original_response(self, **kwargs: Any) -> None:
        self.response._edits.append(kwargs)


# ---------------------------------------------------------------------- 构造 helper


def build_guild(
    *,
    owner_id: int = MODERATOR_ID,
    target_position: int = 10,
    moderator_permissions: discord.Permissions | None = None,
    extra_members: list[FakeMember] | None = None,
    with_mod_log_channel: bool = True,
) -> FakeGuild:
    """一个典型的测试服务器：owner/mod 一人、目标成员一人、机器人一个。"""
    channels = [FakeChannel(MOD_LOG_CHANNEL_ID)] if with_mod_log_channel else []
    members = [
        FakeMember(MODERATOR_ID, 90, name="moderator", permissions=moderator_permissions),
        FakeMember(TARGET_ID, target_position, name="target"),
        FakeMember(BOT_ID, 80, name="bot", bot=True),
    ]
    members.extend(extra_members or [])
    return FakeGuild(owner_id=owner_id, members=members, channels=channels)


def add_message(channel: Any, *, message_id: int = MESSAGE_ID, content: str = "hello") -> FakeMessage:
    """往假频道里塞一条消息，供 ``fetch_message`` 用。"""
    message = FakeMessage(message_id, channel, content=content)
    channel._messages[message_id] = message
    return message


async def setup_bot(settings: Any) -> SyncaindBot:
    """真装配：真正的 SyncaindBot + 真正的模块，只把数据库放在临时目录。

    走 ``SyncaindBot.prepare``（即 setup_hook 去掉联网的 sync 那一步），
    测试与线上就不会因为装配顺序不同而漂移。
    """
    bot = SyncaindBot(settings=settings)
    await bot.prepare()
    await bot.guild_settings.update(GUILD_ID, mod_log_channel_id=MOD_LOG_CHANNEL_ID)
    return bot


async def run_command(bot: SyncaindBot, command_path: str, interaction: FakeInteraction, **params: Any) -> None:
    """直接调用命令的底层协程。

    ``_callback`` 是私有属性，但这是离线跑 app_commands 处理器的唯一入口。
    走这条路会跳过 Discord 的权限检查（那部分由 Discord 自己保证），
    需要验证检查本身的测试请直接跑 ``command.checks``。
    名字里有空格的（如 ``channel create``）会走分组里的子命令。

    第二个参数刻意叫 ``command_path``：很多命令自己就有 ``name`` / ``command`` 之类的参数，
    叫 ``name`` 或 ``command`` 会和 ``**params`` 撞车。
    """
    parts = command_path.split()
    target: Any = bot.tree.get_command(parts[0])
    assert target is not None, f"命令树里没有 {parts[0]}"
    for part in parts[1:]:
        target = next((child for child in target.commands if child.name == part), None)
        assert target is not None, f"命令树里没有 {command_path}"
    if target.binding is None:
        await target._callback(interaction, **params)
    else:
        await target._callback(target.binding, interaction, **params)
