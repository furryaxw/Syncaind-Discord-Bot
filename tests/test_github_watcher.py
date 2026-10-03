"""监听服务（gh-webhook-watcher）的消费面：SSE 帧解析、订阅头、区间检查、事件 → Release。

帧格式照着服务 README 里的真实样式写（`retry:`、`id:`、`data:`、`: heartbeat`、空行分隔），
解析器是纯函数，所以这里可以逐字节地钉。
"""

from __future__ import annotations

import pytest

from bot.integrations.github.watcher import (
    SSE_CONNECT_TIMEOUT_SECONDS,
    SSE_READ_TIMEOUT_SECONDS,
    WatcherClient,
    WatcherError,
    WatcherStream,
    _to_event,
    iter_frames,
    parse_sse_frame,
    release_from_event,
    tag_from_url,
)
from tests.github_fakes import (
    FakeJsonTransport,
    FakeLineStream,
    FakeSseResponse,
    FakeSseSession,
    make_event,
    release_payload,
)

# ---------------------------------------------------------------- 帧解析


def test_parses_a_plain_data_frame() -> None:
    frame = parse_sse_frame(["id: 12", 'data: {"seq":12}'])

    assert frame is not None
    assert frame.event_id == "12"
    assert frame.data == '{"seq":12}'


def test_heartbeat_comments_are_not_frames() -> None:
    """心跳是注释，不是数据 —— 不能当成事件推给上层。"""
    assert parse_sse_frame([": heartbeat 2026-10-01T00:00:00Z"]) is None
    assert parse_sse_frame([]) is None


def test_multi_line_data_is_joined() -> None:
    frame = parse_sse_frame(["data: line one", "data: line two"])

    assert frame is not None
    assert frame.data == "line one\nline two"


def test_retry_and_event_name_are_read() -> None:
    frame = parse_sse_frame(["retry: 5000", "event: release", "data: {}"])

    assert frame is not None
    assert frame.retry_ms == 5000
    assert frame.event == "release"


def test_bad_retry_value_is_ignored() -> None:
    frame = parse_sse_frame(["retry: soon", "data: {}"])

    assert frame is not None and frame.retry_ms is None


def test_a_value_without_a_space_separator_is_kept() -> None:
    frame = parse_sse_frame(['data:{"a":1}'])

    assert frame is not None and frame.data == '{"a":1}'


async def collect(lines: list[str]) -> list[str]:
    """只收**有 data** 的帧：纯 `retry:` / 心跳是控制信息，不是事件。"""
    return [frame.data async for frame in iter_frames(FakeLineStream(lines)) if frame.data]


async def test_a_retry_only_frame_is_a_control_frame_not_an_event() -> None:
    """`retry:` 帧要能被认出来（重连退避靠它），但它不带事件，不该往外发。"""
    frames = [frame async for frame in iter_frames(FakeLineStream(["retry: 3000\n", "\n"]))]

    assert len(frames) == 1
    assert frames[0].retry_ms == 3000
    assert frames[0].data == ""


async def test_frames_are_split_on_blank_lines() -> None:
    lines = ["retry: 3000\n", "\n", "id: 1\n", "data: first\n", "\n", "id: 2\n", "data: second\n", "\n"]

    assert await collect(lines) == ["first", "second"]


async def test_a_trailing_frame_without_a_blank_line_is_flushed() -> None:
    assert await collect(["id: 1\n", "data: only\n"]) == ["only"]


async def test_crlf_is_tolerated() -> None:
    """真实 HTTP 可能用 CRLF 分行。"""
    assert await collect(["data: value\r\n", "\r\n"]) == ["value"]


# ---------------------------------------------------------------- 订阅（SSE）


def make_stream(lines: list[str], *, status: int = 200) -> tuple[WatcherStream, FakeSseSession]:
    session = FakeSseSession(FakeSseResponse(lines, status=status))
    return WatcherStream("http://watch:1", token="tok", session=session), session


async def test_stream_yields_events_and_skips_heartbeats() -> None:
    event = make_event(seq=7)
    lines = [
        ": heartbeat\n",
        "\n",
        "id: 7\n",
        f"data: {__import__('json').dumps(event, ensure_ascii=False)}\n",
        "\n",
    ]
    stream, _session = make_stream(lines)

    events = [item async for item in stream.events(3)]

    assert [item.seq for item in events] == [7]
    assert events[0].full_name == "furryaxw/SprocketModManager"


async def test_stream_resumes_with_last_event_id() -> None:
    stream, session = make_stream([])

    [item async for item in stream.events(42)]

    _url, headers = session.calls[0]
    assert headers["Last-Event-ID"] == "42"
    assert headers["Accept"] == "text/event-stream"
    assert headers["Authorization"] == "Bearer tok"


async def test_stream_overrides_the_timeout_per_request() -> None:
    """SSE 是长连接，不能吃 aiohttp 默认的「请求起 5 分钟」总时限。

    总时限到点就掐断，和连接死没死无关 —— 那会让订阅每 5 分钟断一次。超时必须
    **按请求**覆盖：同一个 session 也在跑 GitHub API 与区间检查的普通请求，
    在 session 级把 total 关掉会让那些请求失去超时保护。
    """
    stream, session = make_stream([])

    [item async for item in stream.events(0)]

    timeout = session.timeouts[0]
    assert timeout is not None, "订阅请求必须显式带上超时"
    assert timeout.total is None, "总时限要关掉，否则订阅每 5 分钟被掐一次"
    assert timeout.sock_connect == SSE_CONNECT_TIMEOUT_SECONDS
    assert timeout.sock_read == SSE_READ_TIMEOUT_SECONDS, "读超时留着，半开的死连接才收得回来"


async def test_bytes_lines_are_decoded_and_framed() -> None:
    """aiohttp 的响应体是 bytes 行。

    只认 str 不会报错，而是**静默失效**：空行判断对 bytes 恒为假，每一帧都被当成
    非空行堆进缓冲，于是既切不出帧、也没有异常 —— 看起来就像服务端什么都没推。
    """

    async def source():
        yield b": heartbeat 2026-10-03T11:20:25Z\n"
        yield b"\n"
        yield b"id: 3\n"
        yield b'data: {"seq": 3}\n'
        yield b"\n"

    frames = [frame async for frame in iter_frames(source())]

    assert len(frames) == 1
    assert frames[0].event_id == "3"
    assert frames[0].data == '{"seq": 3}'


async def test_stream_omits_last_event_id_at_the_beginning() -> None:
    stream, session = make_stream([])

    [item async for item in stream.events(0)]

    _url, headers = session.calls[0]
    assert "Last-Event-ID" not in headers


async def test_stream_url_carries_the_kind_filter() -> None:
    stream, session = make_stream([])

    [item async for item in stream.events(0)]

    url, _headers = session.calls[0]
    assert url == "http://watch:1/api/stream?kind=Release"


async def test_stream_honours_the_server_retry_hint() -> None:
    """重连退避用服务端给的 retry，而不是我们自己拍一个数。"""
    stream, _session = make_stream(["retry: 8000\n", "\n"])

    [item async for item in stream.events(0)]

    assert stream.retry_ms == 8000


async def test_stream_clamps_an_absurd_retry_hint() -> None:
    stream, _session = make_stream(["retry: 999999999\n", "\n"])

    [item async for item in stream.events(0)]

    assert stream.retry_ms == 60_000


async def test_stream_raises_on_a_rejected_subscription() -> None:
    stream, _session = make_stream([], status=401)

    with pytest.raises(WatcherError) as excinfo:
        [item async for item in stream.events(0)]

    assert excinfo.value.status == 401


async def test_a_non_json_frame_is_skipped_not_fatal() -> None:
    good = make_event(seq=2)
    lines = ["data: not json\n", "\n", f"data: {__import__('json').dumps(good)}\n", "\n"]
    stream, _session = make_stream(lines)

    events = [item async for item in stream.events(0)]

    assert [item.seq for item in events] == [2]


# ---------------------------------------------------------------- 区间检查（轮询面）


async def test_fetch_parses_the_page() -> None:
    transport = FakeJsonTransport(
        {
            "/api/events": {
                "events": [make_event(seq=9)],
                "count": 1,
                "cursor": 9,
                "latest_seq": 12,
                "more": True,
                "since_evicted": False,
                "buffer_min_seq": 1,
            }
        }
    )
    client = WatcherClient("http://watch:1/", transport, token="tok")

    page = await client.fetch(5, limit=1)

    assert page.cursor == 9 and page.latest_seq == 12 and page.more is True
    assert page.since_evicted is False
    assert [event.seq for event in page.events] == [9]
    url, headers = transport.calls[0]
    assert url == "http://watch:1/api/events?since=5&limit=1&kind=Release"
    assert headers["Authorization"] == "Bearer tok"


async def test_fetch_surfaces_eviction() -> None:
    """SSE 不会告诉我漏了事件，所以这个字段是订阅前必须先查的东西。"""
    transport = FakeJsonTransport({"/api/events": {"events": [], "cursor": 0, "since_evicted": True}})

    page = await WatcherClient("http://watch:1", transport).fetch(1)

    assert page.since_evicted is True


async def test_fetch_maps_401_to_something_readable() -> None:
    class HttpError(Exception):
        status = 401

    transport = FakeJsonTransport(error=HttpError())
    client = WatcherClient("http://watch:1", transport, token="bad")

    with pytest.raises(WatcherError) as excinfo:
        await client.fetch(0)

    assert excinfo.value.status == 401
    assert "令牌" in str(excinfo.value)


# ---------------------------------------------------------------- 事件 → Release


def test_release_from_a_payload_event() -> None:
    event = _to_event(make_event(seq=3, payload=release_payload(101, tag="v0.6.1", name="大更新", prerelease=True)))

    release = release_from_event(event)

    assert release is not None
    assert (release.repo, release.release_id, release.tag) == ("furryaxw/SprocketModManager", 101, "v0.6.1")
    assert release.body.startswith("## 变更")
    assert release.prerelease is True


def test_without_a_payload_there_is_no_release() -> None:
    """监听服务没开 GHW_INCLUDE_PAYLOAD 时事件里没有正文 —— 返回 None，由调用方去 GitHub 取。"""
    event = _to_event(make_event(seq=3))

    assert release_from_event(event) is None


def test_a_draft_release_is_ignored() -> None:
    event = _to_event(make_event(seq=3, payload=release_payload(101, draft=True)))

    assert release_from_event(event) is None


def test_a_payload_without_id_or_tag_is_ignored() -> None:
    """外部数据可能残缺：宁可少推一条，也不要炸掉整条订阅。"""
    assert release_from_event(_to_event(make_event(seq=1, payload={"release": {"tag_name": "v1"}}))) is None
    assert release_from_event(_to_event(make_event(seq=1, payload={"release": {"id": 5}}))) is None


def test_tag_from_url() -> None:
    assert tag_from_url("https://github.com/o/r/releases/tag/v0.6.1") == "v0.6.1"
    assert tag_from_url("https://github.com/o/r/releases/tag/release%2F1.0") == "release/1.0"
    assert tag_from_url("https://github.com/o/r/releases") is None
    assert tag_from_url("") is None
