"""表单/审核 token：HS256 JWT → AES-256-GCM 加密 → hex。

token 里不放权限，只放「谁、哪个表单、哪个发布、什么时候过期」；服务端解码后还要
重新查数据库（用户是否还在服务器、发布是否开放、提交是否存在）。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
from datetime import datetime, timedelta, timezone
from typing import Any

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

FORM_TTL = timedelta(days=7)
REVIEW_TTL = timedelta(hours=6)
MIN_SECRET_LENGTH = 32
_NONCE_BYTES = 12


class TokenError(ValueError):
    """token 不可用：格式、签名、解密或过期。"""


def issue_form_token(
    secret: str,
    *,
    guild_id: int,
    form_id: str,
    publication_id: int,
    user_id: int,
    locale: str,
    ttl: timedelta = FORM_TTL,
) -> str:
    return _issue(
        secret,
        {
            "typ": "form",
            "sub": int(user_id),
            "gid": int(guild_id),
            "fid": str(form_id),
            "pid": int(publication_id),
            "loc": str(locale),
        },
        ttl,
    )


def issue_review_token(
    secret: str,
    *,
    guild_id: int,
    reviewer_id: int,
    form_id: str | None = None,
    submission_id: int | None = None,
    ttl: timedelta = REVIEW_TTL,
) -> str:
    claims: dict[str, Any] = {
        "typ": "review",
        "sub": int(reviewer_id),
        "gid": int(guild_id),
    }
    if form_id is not None:
        claims["fid"] = str(form_id)
    if submission_id is not None:
        claims["sid"] = int(submission_id)
    return _issue(secret, claims, ttl)


def csrf_token(secret: str, jti: str) -> str:
    return hmac.new(_key(secret, b"csrf"), jti.encode(), hashlib.sha256).hexdigest()


def verify_csrf(secret: str, jti: str, value: str) -> bool:
    expected = csrf_token(secret, jti)
    return bool(value) and hmac.compare_digest(expected, value)


def decode_token(secret: str, token_hex: str) -> dict[str, Any]:
    """解密并校验签名/过期，返回 claims。"""
    _check_secret(secret)
    try:
        raw = bytes.fromhex(token_hex)
    except ValueError as exc:
        raise TokenError("token 不是 hex") from exc
    if len(raw) <= _NONCE_BYTES:
        raise TokenError("token 太短")

    nonce, ciphertext = raw[:_NONCE_BYTES], raw[_NONCE_BYTES:]
    try:
        compact = AESGCM(_key(secret, b"enc")).decrypt(nonce, ciphertext, None)
    except Exception as exc:  # InvalidTag 等
        raise TokenError("token 无法解密") from exc

    parts = compact.decode("ascii", errors="strict").split(".")
    if len(parts) != 3:
        raise TokenError("token 结构不对")
    signing_input = f"{parts[0]}.{parts[1]}".encode()
    expected = hmac.new(_key(secret, b"jwt"), signing_input, hashlib.sha256).digest()
    try:
        signature = _b64decode(parts[2])
    except ValueError as exc:
        raise TokenError("签名编码不对") from exc
    if not hmac.compare_digest(expected, signature):
        raise TokenError("签名不匹配")

    try:
        claims = json.loads(_b64decode(parts[1]))
    except (ValueError, json.JSONDecodeError) as exc:
        raise TokenError("claims 无法解析") from exc
    if not isinstance(claims, dict):
        raise TokenError("claims 不是对象")

    exp = claims.get("exp")
    if not isinstance(exp, int):
        raise TokenError("缺少 exp")
    if exp <= int(datetime.now(timezone.utc).timestamp()):
        raise TokenError("token 已过期")
    if claims.get("typ") not in {"form", "review"}:
        raise TokenError("未知 token 类型")
    return claims


def _issue(secret: str, claims: dict[str, Any], ttl: timedelta) -> str:
    _check_secret(secret)
    now = datetime.now(timezone.utc)
    payload = dict(claims)
    payload["iat"] = int(now.timestamp())
    payload["exp"] = int((now + ttl).timestamp())
    payload["jti"] = secrets.token_hex(8)

    header = {"alg": "HS256", "typ": "JWT"}
    signing_input = (
        f"{_b64encode(json.dumps(header, separators=(',', ':')).encode())}."
        f"{_b64encode(json.dumps(payload, separators=(',', ':')).encode())}"
    )
    signature = hmac.new(_key(secret, b"jwt"), signing_input.encode(), hashlib.sha256).digest()
    compact = f"{signing_input}.{_b64encode(signature)}"

    nonce = secrets.token_bytes(_NONCE_BYTES)
    ciphertext = AESGCM(_key(secret, b"enc")).encrypt(nonce, compact.encode(), None)
    return (nonce + ciphertext).hex()


def _check_secret(secret: str) -> None:
    if len(secret) < MIN_SECRET_LENGTH:
        raise TokenError(f"FORM_TOKEN_SECRET 至少 {MIN_SECRET_LENGTH} 字符")


def _key(secret: str, purpose: bytes) -> bytes:
    return hashlib.sha256(secret.encode() + b"|" + purpose).digest()


def _b64encode(value: bytes) -> str:
    return base64.urlsafe_b64encode(value).rstrip(b"=").decode()


def _b64decode(value: str) -> bytes:
    padded = value + "=" * (-len(value) % 4)
    return base64.urlsafe_b64decode(padded.encode())
