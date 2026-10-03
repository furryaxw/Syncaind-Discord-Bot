"""把一条 release 变成 Discord Embed。

release notes 是**外部文本**：可能带 `@everyone`、可能几千字、可能嵌着机器标记。
这里负责三件事：**清掉看不见的标记**、**截断**、**空正文给个说法**。
禁用 mention 不在这一层做，而在发送处（`allowed_mentions=none`）—— 那是每条消息都必须带的东西。
"""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import discord

from bot.core.clock import parse_iso
from bot.integrations.github.releases import Release

MAX_TITLE = 256
MAX_DESCRIPTION = 4096
# GitHub 的绿与「预发布」的琥珀色
COLOR_STABLE = 0x238636
COLOR_PRERELEASE = 0xB58900

Translator = Callable[..., str]

# HTML 注释：`<!-- ... -->`，可以跨行（DOTALL），非贪婪。
HTML_COMMENT = re.compile(r"<!--.*?-->", re.DOTALL)
BLANK_RUN = re.compile(r"\n{3,}")


def clean_notes(body: str) -> str:
    """去掉 HTML 注释并收拾因此留下的空行。

    为什么是「所有注释」而不是只认 ``sp-compat``：**Discord 不渲染 HTML**，
    任何 ``<!-- ... -->`` 在 GitHub 页面看不见、在 Discord 里却是明文（例如
    ``<!-- sp-compat {"hamish.sprocket": ["0.2.53.x"]} -->``）。
    所以规则就该是「让它看起来和 GitHub 上一样」，以后再加新的机器标记也不用回来改这里。

    只处理**闭合的**注释：遇到没闭合的 ``<!--`` 宁可留着明文，也不要把后面的正文一起吃掉
    —— 少隐藏一点信息，比悄悄吞掉半篇发布说明好。
    """
    cleaned = HTML_COMMENT.sub("", body.replace("\r\n", "\n"))
    return BLANK_RUN.sub("\n\n", cleaned).strip()


def build_release_embed(release: Release, *, t: Translator) -> discord.Embed:
    title = f"{release.repo} {release.tag}".strip()
    if release.prerelease:
        title = f"🧪 {title}"
    if len(title) > MAX_TITLE:
        title = title[: MAX_TITLE - 1] + "…"

    body = clean_notes(release.body)
    embed = discord.Embed(
        title=title,
        description=_clip(body, t) if body else t("feed.empty_body"),
        url=release.html_url or None,
        color=COLOR_PRERELEASE if release.prerelease else COLOR_STABLE,
    )
    timestamp = parse_iso(release.published_at)
    if timestamp is not None:
        embed.timestamp = timestamp
    embed.set_author(name=release.repo)
    return embed


def _clip(body: str, t: Translator) -> str:
    """超长正文截断。

    尽量在最后一个换行处切，免得把一段 markdown 从中间劈开；完整内容靠 embed 标题上的链接。
    """
    if len(body) <= MAX_DESCRIPTION:
        return body
    suffix = t("feed.truncated")
    budget = MAX_DESCRIPTION - len(suffix) - 1
    head = body[:budget]
    cut = head.rfind("\n")
    if cut > budget // 2:
        head = head[:cut]
    return f"{head.rstrip()}\n{suffix}"


def release_kwargs() -> dict[str, Any]:
    """给 `channel.send` 用的固定参数。

    **release notes 里出现 `@everyone` 时绝不能生效** —— 那是外部输入，一条 release 就能把
    整个服务器 @ 一遍。所有发送都必须带上它。
    """
    return {"allowed_mentions": discord.AllowedMentions.none()}
