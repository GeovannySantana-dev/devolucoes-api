import pytest
from cryptography.fernet import Fernet

from app.crypto import PREFIX, TokenCryptoError, decrypt, encrypt

KEY = Fernet.generate_key().decode()


def test_roundtrip_and_prefix():
    enc = encrypt("token-secreto", KEY)
    assert enc.startswith(PREFIX) and "token-secreto" not in enc
    assert decrypt(enc, KEY) == "token-secreto"
    assert encrypt(enc, KEY) == enc  # não cifra duas vezes


def test_plain_values_still_readable_and_none():
    assert decrypt("texto-antigo", KEY) == "texto-antigo"
    assert encrypt(None, KEY) is None and decrypt(None, KEY) is None
    assert encrypt("x", "") == "x"  # sem chave: texto puro (desenvolvimento)


def test_wrong_or_missing_key_fails_clearly():
    enc = encrypt("token", KEY)
    with pytest.raises(TokenCryptoError, match="diferente"):
        decrypt(enc, Fernet.generate_key().decode())
    with pytest.raises(TokenCryptoError, match="não está configurada"):
        decrypt(enc, "")
    with pytest.raises(TokenCryptoError, match="inválida"):
        encrypt("x", "chave-ruim")
