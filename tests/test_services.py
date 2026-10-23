"""服务注册表：跨模块能力的唯一入口。"""

from __future__ import annotations

import pytest

from bot.core.services import ServiceRegistry


def test_register_and_get() -> None:
    registry = ServiceRegistry()
    service = object()

    registry.register("keys", service)

    assert registry.get("keys") is service
    assert registry.require("keys") is service
    assert registry.has("keys")
    assert registry.names() == ("keys",)


def test_duplicate_registration_is_rejected() -> None:
    registry = ServiceRegistry()
    registry.register("keys", object())

    with pytest.raises(ValueError):
        registry.register("keys", object())


def test_unregister_makes_get_return_none() -> None:
    registry = ServiceRegistry()
    registry.register("keys", object())

    registry.unregister("keys")

    assert registry.get("keys") is None
    assert not registry.has("keys")


def test_require_raises_when_missing() -> None:
    with pytest.raises(LookupError):
        ServiceRegistry().require("keys")
