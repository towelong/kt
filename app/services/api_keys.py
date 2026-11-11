"""API Key 的生成、哈希与吊销。完整 key 只在创建时返回一次。"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import timedelta

from sqlmodel import Session

from app import crud
from app.models import ApiKey, utcnow

PREFIX_LENGTH = 11  # "kt_" + 8 位随机串，用于查库和日志展示


def hash_api_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def generate_api_key() -> tuple[str, str]:
    token = "kt_" + secrets.token_urlsafe(32)
    return token, token[:PREFIX_LENGTH]


def create_key(
    session: Session,
    *,
    name: str,
    expires_in_days: int | None = None,
    max_attempts: int = 3,
) -> tuple[ApiKey, str]:
    """返回 (记录, 完整 key)。库里只留前缀和 sha256，明文不落库。"""
    name = name.strip()
    if not name or len(name) > 64:
        raise ValueError("name 必填且不超过 64 字符")
    if expires_in_days is not None and expires_in_days <= 0:
        raise ValueError("expires_in_days 必须是正整数")
    expires_at = utcnow() + timedelta(days=expires_in_days) if expires_in_days is not None else None
    for _ in range(max_attempts):
        token, prefix = generate_api_key()
        try:
            api_key = crud.create_api_key(
                session,
                name=name,
                key_prefix=prefix,
                key_hash=hash_api_key(token),
                expires_at=expires_at,
            )
        except crud.ConflictError:
            continue
        return api_key, token
    raise RuntimeError("生成 API Key 失败：前缀连续冲突")


def revoke_key(session: Session, *, prefix: str) -> ApiKey | None:
    api_key = crud.get_api_key_by_prefix(session, prefix)
    if api_key is None:
        return None
    api_key.enabled = False
    return crud.save_api_key(session, api_key)


def verify_api_key(session: Session, presented: str) -> tuple[ApiKey | None, str | None]:
    """返回 (记录, 失败原因)。失败原因只有 not_found / disabled / expired。"""
    if not presented:
        return None, "not_found"
    api_key = crud.get_api_key_by_prefix(session, presented[:PREFIX_LENGTH])
    if api_key is None or not hmac.compare_digest(api_key.key_hash, hash_api_key(presented)):
        return None, "not_found"
    if not api_key.enabled:
        return None, "disabled"
    if api_key.expires_at is not None and api_key.expires_at <= utcnow():
        return None, "expired"
    crud.touch_api_key(session, api_key)
    return api_key, None
