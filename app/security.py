# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import hashlib
import hmac
import secrets
import time

from fastapi import Depends, HTTPException, Security
from fastapi.security import APIKeyHeader
from sqlalchemy.orm import Session

from .config import get_settings
from .db import get_db

_api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)


def require_api_key(key: str | None = Security(_api_key_header)) -> None:
    if not key or not secrets.compare_digest(key, get_settings().api_key):
        raise HTTPException(status_code=401, detail="X-API-Key inválida")


def require_integration_key(key: str | None = Security(_api_key_header), db: Session = Depends(get_db)) -> None:
    """Chave de integração (pdv_...), somente leitura. Criada e revogada em /app/integracoes."""
    from .services.api_keys import verify_key

    if verify_key(db, key) is None:
        raise HTTPException(status_code=401, detail="X-API-Key inválida ou revogada")


# State assinado para o OAuth (evita CSRF sem precisar guardar estado no servidor)
STATE_TTL_SECONDS = 15 * 60


def make_oauth_state() -> str:
    ts = str(int(time.time()))
    nonce = secrets.token_urlsafe(8)
    sig = hmac.new(get_settings().api_key.encode(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    return f"{ts}.{nonce}.{sig}"


def pkce_verifier_for(state: str) -> str:
    """Code verifier do PKCE derivado do state: não trafega na URL e não precisa ser guardado."""
    return hmac.new(get_settings().api_key.encode(), f"pkce:{state}".encode(), hashlib.sha256).hexdigest()


def check_oauth_state(state: str) -> bool:
    try:
        ts, nonce, sig = state.split(".")
    except ValueError:
        return False
    expected = hmac.new(get_settings().api_key.encode(), f"{ts}.{nonce}".encode(), hashlib.sha256).hexdigest()[:32]
    return hmac.compare_digest(sig, expected) and time.time() - int(ts) < STATE_TTL_SECONDS
