# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Usuários das telas da equipe: senha (scrypt) e sessão em cookie assinado."""

import base64
import hashlib
import hmac
import secrets
import time
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import User

ROLES = {
    "operador": "Operador",
    "gestor": "Gestor",
}
# O que cada perfil pode fazer nas telas. A mesma pessoa confere o pacote e decide no marketplace.
_OPERATOR = {"inspect", "media", "note", "decide", "erp"}
PERMISSIONS = {
    "operador": _OPERATOR,
    "gestor": _OPERATOR | {"users", "reports"},
}

_SCRYPT = {"n": 2**14, "r": 8, "p": 1}
SESSION_COOKIE = "devolucoes_sessao"


class AuthError(Exception):
    pass


def can(user: User | None, permission: str) -> bool:
    return bool(user and user.active and permission in PERMISSIONS.get(user.role, set()))


# ---------- senha ----------

def hash_password(password: str) -> str:
    if len(password) < 8:
        raise AuthError("A senha precisa ter pelo menos 8 caracteres")
    salt = secrets.token_bytes(16)
    digest = hashlib.scrypt(password.encode(), salt=salt, **_SCRYPT)
    return "scrypt$" + base64.b64encode(salt).decode() + "$" + base64.b64encode(digest).decode()


def verify_password(password: str, stored: str) -> bool:
    try:
        _, salt_b64, digest_b64 = stored.split("$")
        expected = base64.b64decode(digest_b64)
        digest = hashlib.scrypt(password.encode(), salt=base64.b64decode(salt_b64), **_SCRYPT)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(digest, expected)


# ---------- sessão ----------

def _secret() -> bytes:
    s = get_settings()
    return (s.session_secret or f"sessao:{s.api_key}").encode()


def make_session(user: User) -> str:
    expires = int(time.time()) + get_settings().session_days * 86400
    sig = hmac.new(_secret(), _session_payload(user, expires).encode(), hashlib.sha256).hexdigest()
    return f"{user.id}.{expires}.{sig}"


def _session_payload(user: User, expires: int | str) -> str:
    # Inclui um pedaço do hash da senha (trocar a senha derruba as sessões antigas) e a versão das sessões
    # ("sair de todos os aparelhos" soma 1 e as sessões assinadas antes deixam de valer)
    return f"{user.id}.{expires}.{user.password_hash[-12:]}.{user.session_version or 0}"


def end_all_sessions(db: Session, user: User) -> None:
    user.session_version = (user.session_version or 0) + 1
    db.commit()


def user_from_session(db: Session, token: str | None) -> User | None:
    if not token:
        return None
    try:
        user_id, expires, sig = token.split(".")
        if int(expires) < time.time():
            return None
        user = db.get(User, uuid.UUID(user_id))
    except (ValueError, TypeError):
        return None
    if not user or not user.active:
        return None
    expected = hmac.new(_secret(), _session_payload(user, expires).encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(sig, expected) else None


# ---------- "Esqueci minha senha": link por e-mail ----------
# O link carrega um pedaço do hash da senha atual: depois de usado (a senha muda) ele deixa de valer.

RESET_MINUTES = 30


def make_reset_token(user: User, now: float | None = None) -> str:
    expires = int(now or time.time()) + RESET_MINUTES * 60
    payload = f"reset.{user.id}.{expires}.{user.password_hash[-12:]}"
    sig = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return f"{user.id}.{expires}.{sig}"


def user_from_reset_token(db: Session, token: str | None) -> User | None:
    try:
        user_id, expires, sig = (token or "").split(".")
        if int(expires) < time.time():
            return None
        user = db.get(User, uuid.UUID(user_id))
    except (ValueError, TypeError):
        return None
    if not user or not user.active:
        return None
    payload = f"reset.{user.id}.{expires}.{user.password_hash[-12:]}"
    expected = hmac.new(_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return user if hmac.compare_digest(sig, expected) else None


def find_active_user(db: Session, email: str) -> User | None:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    return user if user and user.active else None


def change_own_password(db: Session, user: User, current: str, new: str, repeat: str) -> None:
    if not verify_password(current, user.password_hash):
        raise AuthError("A senha atual não confere")
    if new != repeat:
        raise AuthError("A senha nova e a repetição não são iguais")
    if new == current:
        raise AuthError("A senha nova precisa ser diferente da atual")
    set_password(db, user, new)


# ---------- cadastro e login ----------

def authenticate(db: Session, email: str, password: str) -> User:
    user = db.scalar(select(User).where(func.lower(User.email) == email.strip().lower()))
    # Mesma mensagem para e-mail inexistente e senha errada
    if not user or not user.active or not verify_password(password, user.password_hash):
        raise AuthError("E-mail ou senha incorretos")
    user.last_login_at = datetime.now(UTC)
    db.commit()
    return user


def create_user(db: Session, email: str, name: str, role: str, password: str) -> User:
    if role not in ROLES:
        raise AuthError(f"Perfil inválido: {role}. Use: {', '.join(ROLES)}")
    email = email.strip().lower()
    if db.scalar(select(User).where(func.lower(User.email) == email)):
        raise AuthError(f"Já existe usuário com o e-mail {email}")
    user = User(email=email, name=name.strip(), role=role, password_hash=hash_password(password), active=True)
    db.add(user)
    db.commit()
    return user


def set_password(db: Session, user: User, password: str) -> None:
    user.password_hash = hash_password(password)
    db.commit()
