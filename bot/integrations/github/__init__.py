"""GitHub 集成：设备流登录、账号映射存储。

这一层不认识 Discord，也不认识 Cog——它只提供「开始设备流 / 轮询 / 取用户」和
「映射表读写」，所以可以完全离线测试。
"""

from .client import (
    STATUS_DONE,
    STATUS_PENDING,
    STATUS_SLOW_DOWN,
    AiohttpTransport,
    DeviceCode,
    DeviceFlowError,
    GitHubClient,
    GitHubUser,
    PollResult,
    Transport,
    WebFlowError,
)
from .releases import (
    MAX_RELEASES,
    GitHubReleaseSource,
    Release,
    ReleaseSource,
    ReleaseSourceError,
)
from .state import DEFAULT_TTL_SECONDS, PendingLink, StateStore
from .store import GitHubAccount, GitHubAccountConflict, GitHubAccountStore
from .targets import ReleaseTarget, ReleaseTargetStore, WatcherCursorStore
from .watcher import (
    WatcherClient,
    WatcherError,
    WatcherEvent,
    WatcherPage,
    WatcherSource,
    WatcherStream,
    WatcherStreamSource,
    parse_sse_frame,
    release_from_event,
    tag_from_url,
)

__all__ = [
    "DEFAULT_TTL_SECONDS",
    "MAX_RELEASES",
    "STATUS_DONE",
    "STATUS_PENDING",
    "STATUS_SLOW_DOWN",
    "AiohttpTransport",
    "DeviceCode",
    "DeviceFlowError",
    "GitHubAccount",
    "GitHubAccountConflict",
    "GitHubAccountStore",
    "GitHubClient",
    "GitHubReleaseSource",
    "GitHubUser",
    "PendingLink",
    "PollResult",
    "Release",
    "ReleaseSource",
    "ReleaseSourceError",
    "ReleaseTarget",
    "ReleaseTargetStore",
    "StateStore",
    "Transport",
    "WatcherClient",
    "WatcherCursorStore",
    "WatcherError",
    "WatcherEvent",
    "WatcherPage",
    "WatcherSource",
    "WatcherStream",
    "WatcherStreamSource",
    "WebFlowError",
    "parse_sse_frame",
    "release_from_event",
    "tag_from_url",
]
