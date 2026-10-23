"""表单 token：加密、hex、claims、过期与防篡改。"""

from __future__ import annotations

from datetime import timedelta

import pytest

from bot.modules.forms.tokens import (
    TokenError,
    decode_token,
    issue_form_token,
    issue_review_token,
)

SECRET = "s" * 40


def test_form_token_round_trip() -> None:
    token = issue_form_token(
        SECRET,
        guild_id=100,
        form_id="team",
        publication_id=7,
        user_id=200,
        locale="zh-CN",
    )

    claims = decode_token(SECRET, token)

    assert claims["typ"] == "form"
    assert claims["gid"] == 100
    assert claims["fid"] == "team"
    assert claims["pid"] == 7
    assert claims["sub"] == 200
    assert claims["loc"] == "zh-CN"
    assert claims["jti"]


def test_token_is_hex() -> None:
    token = issue_form_token(
        SECRET, guild_id=1, form_id="a", publication_id=1, user_id=2, locale="en-US"
    )

    assert all(char in "0123456789abcdef" for char in token)
    assert len(token) > 100


def test_review_token_has_review_type() -> None:
    token = issue_review_token(SECRET, guild_id=100, reviewer_id=300, form_id="team")
    claims = decode_token(SECRET, token)

    assert claims["typ"] == "review"
    assert claims["sub"] == 300
    assert claims["fid"] == "team"


def test_wrong_secret_fails() -> None:
    token = issue_form_token(
        SECRET, guild_id=1, form_id="a", publication_id=1, user_id=2, locale="en-US"
    )

    with pytest.raises(TokenError):
        decode_token("x" * 40, token)


def test_tampered_hex_fails() -> None:
    token = issue_form_token(
        SECRET, guild_id=1, form_id="a", publication_id=1, user_id=2, locale="en-US"
    )
    tampered = token[:-1] + ("0" if token[-1] != "0" else "1")

    with pytest.raises(TokenError):
        decode_token(SECRET, tampered)


def test_expired_token_fails() -> None:
    token = issue_form_token(
        SECRET,
        guild_id=1,
        form_id="a",
        publication_id=1,
        user_id=2,
        locale="en-US",
        ttl=timedelta(seconds=-1),
    )

    with pytest.raises(TokenError):
        decode_token(SECRET, token)


def test_short_secret_rejected() -> None:
    with pytest.raises(TokenError):
        issue_form_token("short", guild_id=1, form_id="a", publication_id=1, user_id=2, locale="en-US")
