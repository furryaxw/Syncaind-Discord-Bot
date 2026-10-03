"""从 GitHub 读 release：解析、分页、草稿过滤、错误映射，以及带不带 token 的区别。"""

from __future__ import annotations

import pytest

from bot.integrations.github.releases import (
    PER_PAGE,
    GitHubReleaseSource,
    ReleaseSourceError,
)
from tests.github_fakes import FakeJsonTransport


class HttpError(Exception):
    """模拟 aiohttp 的响应异常（只用到 ``status`` 这个形状）。"""

    def __init__(self, status: int) -> None:
        super().__init__(f"HTTP {status}")
        self.status = status


def payload(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "id": 101,
        "tag_name": "v1.2.3",
        "name": "修了一堆",
        "body": "## 变更\n- 修了 A\n- 加了 B",
        "html_url": "https://github.com/o/r/releases/tag/v1.2.3",
        "published_at": "2026-09-30T13:52:09Z",
        "prerelease": False,
        "draft": False,
    }
    base.update(overrides)
    return base


def make_source(transport: FakeJsonTransport, token: str | None = None) -> GitHubReleaseSource:
    return GitHubReleaseSource(transport, token=token, user_agent="TestAgent")


async def test_parses_a_release() -> None:
    transport = FakeJsonTransport({"releases": [payload()]})

    releases = await make_source(transport).list_releases("o/r")

    assert len(releases) == 1
    release = releases[0]
    assert (release.repo, release.release_id, release.tag) == ("o/r", 101, "v1.2.3")
    assert release.title == "修了一堆"
    assert "修了 A" in release.body
    assert release.published_at.startswith("2026-09-30")
    assert release.prerelease is False


async def test_title_falls_back_to_the_tag() -> None:
    transport = FakeJsonTransport({"releases": [payload(name="")]})

    releases = await make_source(transport).list_releases("o/r")

    assert releases[0].title == "v1.2.3"


async def test_prereleases_are_kept_but_flagged() -> None:
    """预发布也要推，只是要标出来——作者常常靠它让用户先试。"""
    transport = FakeJsonTransport({"releases": [payload(prerelease=True)]})

    releases = await make_source(transport).list_releases("o/r")

    assert releases[0].prerelease is True


async def test_drafts_are_dropped() -> None:
    transport = FakeJsonTransport({"releases": [payload(id=1), payload(id=2, draft=True)]})

    releases = await make_source(transport).list_releases("o/r")

    assert [release.release_id for release in releases] == [1]


async def test_missing_fields_do_not_explode() -> None:
    """外部 JSON 不受我们控制：字段缺了也要读得出来，而不是 KeyError。"""
    transport = FakeJsonTransport({"releases": [{"id": 5, "tag_name": "v0"}]})

    release = (await make_source(transport).list_releases("o/r"))[0]

    assert release.body == ""
    assert release.html_url == ""
    assert release.published_at == ""
    assert release.prerelease is False


async def test_paginates_when_a_page_is_full() -> None:
    first = [payload(id=index) for index in range(PER_PAGE)]
    # 键必须是**无歧义**的片段：`"page=1"` 会被 `per_page=100` 命中（子串匹配的老毛病）。
    transport = FakeJsonTransport({"&page=1": first, "&page=2": [payload(id=999)]})

    releases = await make_source(transport).list_releases("o/r")

    assert len(releases) == PER_PAGE + 1
    assert releases[-1].release_id == 999
    assert [url for url, _headers in transport.calls] == [
        "https://api.github.com/repos/o/r/releases?per_page=100&page=1",
        "https://api.github.com/repos/o/r/releases?per_page=100&page=2",
    ]


async def test_stops_after_a_short_page() -> None:
    transport = FakeJsonTransport({"&page=1": [payload()]})

    await make_source(transport).list_releases("o/r")

    assert len(transport.calls) == 1


async def test_the_limit_is_respected() -> None:
    transport = FakeJsonTransport({"releases": [payload(id=index) for index in range(10)]})

    releases = await make_source(transport).list_releases("o/r", limit=3)

    assert [release.release_id for release in releases] == [0, 1, 2]


async def test_anonymous_requests_carry_no_authorization() -> None:
    transport = FakeJsonTransport({"releases": []})

    await make_source(transport).list_releases("o/r")

    _url, headers = transport.calls[0]
    assert "Authorization" not in headers
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["X-GitHub-Api-Version"]


async def test_a_token_is_sent_when_configured() -> None:
    transport = FakeJsonTransport({"releases": []})

    await make_source(transport, token="tok").list_releases("o/r")

    _url, headers = transport.calls[0]
    assert headers["Authorization"] == "Bearer tok"


async def test_404_names_the_repository() -> None:
    transport = FakeJsonTransport(error=HttpError(404))

    with pytest.raises(ReleaseSourceError) as excinfo:
        await make_source(transport).list_releases("o/nope")

    assert excinfo.value.status == 404
    assert "o/nope" in str(excinfo.value)


async def test_403_points_at_rate_limiting() -> None:
    transport = FakeJsonTransport(error=HttpError(403))

    with pytest.raises(ReleaseSourceError) as excinfo:
        await make_source(transport).list_releases("o/r")

    assert excinfo.value.status == 403
    assert "限流" in str(excinfo.value)


async def test_unexpected_errors_are_wrapped_not_leaked() -> None:
    """上层只该看到 ReleaseSourceError，不该被迫 import aiohttp。"""
    transport = FakeJsonTransport(error=RuntimeError("boom"))

    with pytest.raises(ReleaseSourceError) as excinfo:
        await make_source(transport).list_releases("o/r")

    assert excinfo.value.status is None
