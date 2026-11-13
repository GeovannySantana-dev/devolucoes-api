"""Chaves de API de integração (somente leitura) para outros sistemas internos.

A chave completa só existe no momento da criação; no banco fica o hash SHA-256 (a chave é aleatória
e longa, então o SHA-256 basta, sem necessidade de bcrypt) e um prefixo curto para identificar.
"""

import hashlib
import secrets
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import ApiKey

KEY_PREFIX = "pdv_"
PREFIX_LEN = 12
LAST_USED_EVERY = timedelta(minutes=5)  # não grava "último uso" a cada chamada


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def create_key(db: Session, name: str, created_by: str | None = None) -> tuple[ApiKey, str]:
    name = name.strip()
    if not name:
        raise ValueError("Dê um nome para a chave (ex.: o sistema que vai usar).")
    token = KEY_PREFIX + secrets.token_urlsafe(32)
    key = ApiKey(name=name[:80], prefix=token[:PREFIX_LEN], key_hash=_hash(token), created_by=created_by)
    db.add(key)
    db.commit()
    return key, token


def verify_key(db: Session, token: str | None) -> ApiKey | None:
    if not token or not token.startswith(KEY_PREFIX):
        return None
    key = db.scalar(select(ApiKey).where(ApiKey.key_hash == _hash(token), ApiKey.revoked_at.is_(None)))
    if key is None:
        return None
    now = datetime.now(UTC)
    if key.last_used_at is None or now - key.last_used_at > LAST_USED_EVERY:
        key.last_used_at = now
        db.commit()
    return key


def revoke_key(db: Session, key_id) -> ApiKey | None:
    key = db.get(ApiKey, key_id)
    if key and key.revoked_at is None:
        key.revoked_at = datetime.now(UTC)
        db.commit()
    return key


def list_keys(db: Session) -> list[ApiKey]:
    return list(db.scalars(select(ApiKey).order_by(ApiKey.revoked_at.is_not(None), ApiKey.created_at.desc())))
