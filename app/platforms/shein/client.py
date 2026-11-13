# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Cliente da SHEIN Open Platform (modo Autooperação).

Autorização (Manual da aplicação de autorização da loja):
1. O admin da conta principal, logado no Seller Center, abre
   `https://openapi-sem.sheincorp.com/#/empower?appid=..&redirectUrl=<base64>&state=..`.
2. A Shein redireciona para o app com `tempToken` (vale 10 min) e `state`.
3. O app troca o tempToken em `/open-api/auth/get-by-token` (assinado com APP_ID + APP_SECRET) e recebe
   `openKeyId` e `secretKey` da loja. O secretKey vem cifrado (AES-128-CBC; chave = 16 primeiros caracteres
   do APP_SECRET; IV = "space-station-de"; base64).

Assinatura: `random(5) + base64(hex(HMAC-SHA256("<id>&<timestamp>&<path>", <segredo> + random(5))))`, onde
id/segredo são APP_ID/APP_SECRET na troca do token e openKeyId/secretKey nas chamadas da loja.
"""

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

import httpx
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from ..base import PlatformError

GET_BY_TOKEN = "/open-api/auth/get-by-token"
IV = b"space-station-de"
# Erros que pedem ação de alguém da loja: vão no aviso por e-mail com o que fazer
HINTS = {
    "openapi00001": "chave da loja inválida (a Shein não aceitou a assinatura). Autorize a loja Shein de novo no app",
    "openapi00002": "o IP do servidor não está liberado na Shein. Adicione o IP na lista branca do aplicativo "
                    "no Open Platform (o IP de internet da empresa pode ter mudado)",
}


def sign(key_id: str, secret: str, path: str, timestamp: str, random_key: str | None = None) -> str:
    random_key = random_key or secrets.token_hex(3)[:5]
    message = f"{key_id}&{timestamp}&{path}".encode()
    digest = hmac.new((secret + random_key).encode(), message, hashlib.sha256).hexdigest()
    return random_key + base64.b64encode(digest.encode()).decode()


def decrypt_secret(encrypted: str, app_secret: str) -> str:
    cipher = Cipher(algorithms.AES(app_secret.encode()[:16]), modes.CBC(IV))
    dec = cipher.decryptor()
    raw = dec.update(base64.b64decode(encrypted)) + dec.finalize()
    unpadder = padding.PKCS7(128).unpadder()
    return (unpadder.update(raw) + unpadder.finalize()).decode()


class SheinClient:
    def __init__(self, app_id: str, app_secret: str, api_base: str, auth_url: str,
                 http: httpx.Client | None = None, min_interval: float = 0.25):
        self.app_id = app_id
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.auth_url = auth_url
        self.http = http or httpx.Client(timeout=30)
        self.min_interval = min_interval  # limite da Shein: 5 chamadas/s por loja
        self._last = 0.0

    def _check(self) -> None:
        if not self.app_id or not self.app_secret:
            raise PlatformError("SHEIN_APP_ID / SHEIN_APP_SECRET não configurados")

    def authorize_url(self, redirect_url: str, state: str) -> str:
        self._check()
        redirect = base64.b64encode(redirect_url.encode()).decode()
        return f"{self.auth_url}?appid={self.app_id}&redirectUrl={redirect}&state={state}"

    def _post(self, path: str, body: dict[str, Any], headers: dict[str, str]) -> Any:
        wait = self._last + self.min_interval - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        try:
            resp = self.http.post(self.api_base + path, content=json.dumps(body), headers={
                "Content-Type": "application/json;charset=UTF-8", **headers})
        except httpx.TransportError as exc:
            raise PlatformError(f"Falha de conexão com a Shein: {exc}") from exc
        try:
            data = resp.json()
        except ValueError as exc:
            raise PlatformError(f"Shein HTTP {resp.status_code}: {resp.text[:300]}") from exc
        if resp.status_code >= 400 or str(data.get("code")) not in ("0", "200"):
            code = str(data.get("code"))
            hint = HINTS.get(code)
            if hint:
                raise PlatformError(f"Shein: {hint} [{code}: {data.get('msg')}] ({path})")
            raise PlatformError(f"Shein erro {code}: {data.get('msg')} ({path})")
        return data.get("info")

    def exchange_temp_token(self, temp_token: str) -> dict[str, str]:
        """tempToken -> {openKeyId, secretKey (já decifrado)}."""
        self._check()
        ts = str(int(time.time() * 1000))
        info = self._post(GET_BY_TOKEN, {"tempToken": temp_token}, {
            "x-lt-appid": self.app_id,
            "x-lt-timestamp": ts,
            "x-lt-signature": sign(self.app_id, self.app_secret, GET_BY_TOKEN, ts),
        }) or {}
        if not info.get("openKeyId") or not info.get("secretKey"):
            raise PlatformError("A Shein não devolveu as chaves da loja")
        return {"open_key_id": info["openKeyId"], "secret_key": decrypt_secret(info["secretKey"], self.app_secret)}

    def store_request(self, path: str, body: dict[str, Any], open_key_id: str, secret_key: str) -> Any:
        """Chamada em nome da loja autorizada."""
        ts = str(int(time.time() * 1000))
        return self._post(path, body, {
            "x-lt-openKeyId": open_key_id,
            "x-lt-timestamp": ts,
            "x-lt-signature": sign(open_key_id, secret_key, path, ts),
        })
