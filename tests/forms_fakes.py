"""表单测试用的替身与样例定义。

**附件用真的 ``discord.Attachment``**，只把 HTTP 抓取那一层换掉（``state.http.get_from_cdn``）：
自己造一个假附件就会绕过 ``Attachment.read()`` 这条真实路径，而「弹窗一提交就把附件落盘」
正是这里最需要验证的一步。

表单本身是**数据文件**，所以测试也要写文件：:func:`write_test_forms` 往给定目录里放三个表单，
文本与 ``data/forms/`` 里的示例一致（测试断言就按这些文字写）。
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace

import discord

from tests.discord_fakes import (
    MODERATOR_ID,
    TARGET_ID,
    FakeChannel,
    FakeGuild,
    FakeMember,
    FakeRole,
    build_guild,
)

APPLICANT_ID = TARGET_ID
REVIEWER_ID = 700
REVIEW_CHANNEL_ID = 6101
NOTIFY_CHANNEL_ID = 6102
APPLY_CHANNEL_ID = 6103


class CdnHttp:
    """只实现 ``get_from_cdn`` —— ``Attachment.read()`` 走的就是这一条。"""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self._payloads = dict(payloads)

    async def get_from_cdn(self, url: str) -> bytes:
        if url not in self._payloads:
            raise discord.NotFound(SimpleNamespace(status=404, reason="Not Found"), "attachment is gone")
        return self._payloads[url]


class CdnState:
    """只要有 ``http`` 就够了：真 ``Attachment`` 只从这里取 HTTP 层。"""

    def __init__(self, payloads: dict[str, bytes]) -> None:
        self.http = CdnHttp(payloads)


def make_attachment(
    *,
    attachment_id: int = 1,
    filename: str = "sample.png",
    data: bytes = b"binary",
    content_type: str | None = "image/png",
    fetchable: bool = True,
) -> discord.Attachment:
    """一个真的 :class:`discord.Attachment`；``fetchable=False`` 模拟「链接已经失效」。"""
    url = f"https://cdn.example.invalid/{attachment_id}/{filename}"
    payloads = {url: data} if fetchable else {}
    return discord.Attachment(
        data={
            "id": attachment_id,
            "size": len(data),
            "filename": filename,
            "url": url,
            "proxy_url": url,
            "content_type": content_type,
        },
        state=CdnState(payloads),
    )


@dataclass
class FormsWorld:
    """一个装了「审核频道 + 通知频道 + 申请卡片频道 + 两个角色」的服务器。"""

    guild: FakeGuild
    applicant: FakeMember
    reviewer: FakeMember
    review_role: FakeRole
    grant_role: FakeRole
    review_channel: FakeChannel
    notify_channel: FakeChannel
    apply_channel: FakeChannel


async def build_forms_world() -> FormsWorld:
    guild = build_guild(owner_id=MODERATOR_ID, extra_members=[FakeMember(REVIEWER_ID, 20, name="reviewer")])
    channels = {
        REVIEW_CHANNEL_ID: FakeChannel(REVIEW_CHANNEL_ID, name="review"),
        NOTIFY_CHANNEL_ID: FakeChannel(NOTIFY_CHANNEL_ID, name="notify"),
        APPLY_CHANNEL_ID: FakeChannel(APPLY_CHANNEL_ID, name="apply"),
    }
    for channel in channels.values():
        channel.guild = guild
        guild.channels.append(channel)
        guild._channels[channel.id] = channel

    review_role = await guild.create_role("reviewer")
    grant_role = await guild.create_role("team")
    applicant = guild.get_member(APPLICANT_ID)
    reviewer = guild.get_member(REVIEWER_ID)
    assert applicant is not None and reviewer is not None
    await reviewer.add_roles(review_role)

    return FormsWorld(
        guild=guild,
        applicant=applicant,
        reviewer=reviewer,
        review_role=review_role,
        grant_role=grant_role,
        review_channel=channels[REVIEW_CHANNEL_ID],
        notify_channel=channels[NOTIFY_CHANNEL_ID],
        apply_channel=channels[APPLY_CHANNEL_ID],
    )


# ---------------------------------------------------------------------- 样例表单（数据文件）


def _text(english: str, chinese: str) -> dict[str, str]:
    return {"en-US": english, "zh-CN": chinese}


TEAM_APPLICATION: dict[str, object] = {
    "id": "team_application",
    "mode": "apply",
    "title": _text("Team application", "团队申请"),
    "description": _text("Apply here.", "想加入团队就在这里申请。"),
    "actions": ["grant_role", "notify_channel"],
    "fields": [
        {
            "key": "work",
            "kind": "choice",
            "label": _text("What kind of work", "想做哪类工作"),
            "options": [
                {"value": "translate", "label": _text("Translation", "翻译")},
                {"value": "test", "label": _text("Testing", "测试")},
                {"value": "pack", "label": _text("Packing", "整合包")},
            ],
        },
        {"key": "availability", "kind": "short", "label": _text("Time per week", "每周时间"), "required": False},
        {"key": "experience", "kind": "long", "label": _text("Relevant experience", "相关经验")},
        {"key": "portfolio", "kind": "short", "label": _text("Link to your work", "作品链接"), "required": False},
        {
            "key": "samples",
            "kind": "file",
            "label": _text("Sample files", "作品文件"),
            "max_files": 3,
            "required": False,
        },
        {"key": "contact", "kind": "short", "label": _text("How to reach you", "怎么联系你"), "required": False},
    ],
}

EVENT_SIGNUP: dict[str, object] = {
    "id": "event_signup",
    "mode": "signup",
    "title": _text("Event signup", "活动报名"),
    "description": _text("Sign up for the event.", "报名参加活动。"),
    "fields": [
        {
            "key": "slot",
            "kind": "choice",
            "label": _text("Which session", "参加哪一场"),
            "options": [
                {"value": "day1", "label": _text("First session", "第一场")},
                {"value": "day2", "label": _text("Second session", "第二场")},
            ],
        },
        {"key": "note", "kind": "short", "label": _text("Note", "备注"), "required": False},
    ],
}

FEEDBACK: dict[str, object] = {
    "id": "feedback",
    "mode": "collect",
    "title": _text("Feedback", "意见收集"),
    "description": _text("Anything you want to say.", "有什么想说的都可以写。"),
    "allow_multiple": True,
    "fields": [
        {
            "key": "topics",
            "kind": "multi",
            "label": _text("About what", "关于什么"),
            "options": [
                {"value": "mods", "label": _text("Mods", "模组")},
                {"value": "server", "label": _text("Server", "服务器")},
                {"value": "events", "label": _text("Events", "活动")},
                {"value": "other", "label": _text("Other", "其他")},
            ],
        },
        {"key": "detail", "kind": "long", "label": _text("Tell me more", "具体说说")},
        {
            "key": "screenshots",
            "kind": "file",
            "label": _text("Screenshots", "截图"),
            "max_files": 3,
            "required": False,
        },
    ],
}

TEST_FORMS = (EVENT_SIGNUP, FEEDBACK, TEAM_APPLICATION)


def write_test_forms(directory: Path) -> Path:
    """把样例表单写进目录（文件名 = id），供模块加载或测试直接读取。"""
    directory.mkdir(parents=True, exist_ok=True)
    for payload in TEST_FORMS:
        name = f"{payload['id']}.json"
        (directory / name).write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return directory
