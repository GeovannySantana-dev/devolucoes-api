"""Criptografia dos tokens das lojas no banco (Fernet: AES-128 + HMAC).

Com TOKEN_ENCRYPTION_KEY definida, `access_token`/`refresh_token` são gravados como "enc:v1:<texto cifrado>".
Valores antigos em texto puro continuam sendo lidos (e são cifrados na próxima gravação ou com
scripts/criptografar_tokens.py). Sem a chave, grava em texto puro (desenvolvimento).

⚠ Perder a chave = ter de autorizar todas as lojas de novo (os tokens não podem ser lidos).
Gerar uma chave: python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
"""

from functools import lru_cache

from cryptography.fernet import Fernet, InvalidToken
from sqlalchemy.types import Text, TypeDecorator

from .config import get_settings

PREFIX = "enc:v1:"


class TokenCryptoError(Exception):
    pass


@lru_cache
def _fernet(key: str) -> Fernet:
    try:
        return Fernet(key.encode())
    except (ValueError, TypeError) as exc:
        raise TokenCryptoError("TOKEN_ENCRYPTION_KEY inválida (precisa ser uma chave Fernet)") from exc


def encrypt(value: str | None, key: str | None = None) -> str | None:
    key = key if key is not None else get_settings().token_encryption_key
    if value is None or not key or value.startswith(PREFIX):
        return value
    return PREFIX + _fernet(key).encrypt(value.encode()).decode()


def decrypt(value: str | None, key: str | None = None) -> str | None:
    if value is None or not value.startswith(PREFIX):
        return value  # texto puro (antes da criptografia)
    key = key if key is not None else get_settings().token_encryption_key
    if not key:
        raise TokenCryptoError("Token criptografado no banco, mas TOKEN_ENCRYPTION_KEY não está configurada")
    try:
        return _fernet(key).decrypt(value[len(PREFIX):].encode()).decode()
    except InvalidToken as exc:
        raise TokenCryptoError("Não foi possível decifrar o token: TOKEN_ENCRYPTION_KEY diferente da usada") from exc


class EncryptedText(TypeDecorator):
    """Coluna de texto que é cifrada ao gravar e decifrada ao ler."""

    impl = Text
    cache_ok = True

    def process_bind_param(self, value, dialect):
        return encrypt(value)

    def process_result_value(self, value, dialect):
        return decrypt(value)
