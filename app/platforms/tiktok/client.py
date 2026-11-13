# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Cliente HTTP da TikTok Shop Open API (versão 202309).

Docs: https://partner.tiktokshop.com/docv2/page/return-refund-api-overview
"""

import hashlib
import hmac
import json
import logging
import time
from datetime import UTC, datetime
from typing import Any

import httpx

from ..base import PlatformError, TokenSet

log = logging.getLogger(__name__)

RETURNS_SEARCH = "/return_refund/202309/returns/search"
RETURN_APPROVE = "/return_refund/202309/returns/{return_id}/approve"
RETURN_REJECT = "/return_refund/202309/returns/{return_id}/reject"
RETURN_RECORDS = "/return_refund/202309/returns/{return_id}/records"
REJECT_REASONS = "/return_refund/202309/reject_reasons"
AUTHORIZED_SHOPS = "/authorization/202309/shops"
IMAGE_UPLOAD = "/product/202309/images/upload"
# Código do TikTok quando o app não tem o escopo exigido pelo endpoint
SCOPE_DENIED = 105005

# Parâmetros que não entram na assinatura
_UNSIGNED_PARAMS = {"sign", "access_token"}


class TikTokAPIError(PlatformError):
    def __init__(self, code: int | str, message: str, request_id: str | None = None):
        self.code = code
        self.request_id = request_id
        super().__init__(f"TikTok API erro {code}: {message} (request_id={request_id})")


def generate_sign(path: str, params: dict[str, str], body: str, app_secret: str) -> str:
    """Assinatura HMAC-SHA256 exigida em toda chamada da Open API."""
    base = path + "".join(f"{k}{params[k]}" for k in sorted(params) if k not in _UNSIGNED_PARAMS)
    base += body
    wrapped = f"{app_secret}{base}{app_secret}"
    return hmac.new(app_secret.encode(), wrapped.encode(), hashlib.sha256).hexdigest()


def verify_webhook_signature(app_key: str, app_secret: str, raw_body: bytes, signature: str) -> bool:
    """Webhooks do TikTok Shop chegam com header Authorization = HMAC-SHA256(app_key + body)."""
    if not signature:
        return False
    expected = hmac.new(app_secret.encode(), app_key.encode() + raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature.strip())


class TikTokClient:
    def __init__(
        self,
        app_key: str,
        app_secret: str,
        api_base: str,
        auth_base: str,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.app_key = app_key
        self.app_secret = app_secret
        self.api_base = api_base.rstrip("/")
        self.auth_base = auth_base.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.max_retries = max_retries

    # ---------- infraestrutura ----------

    def request(
        self,
        method: str,
        path: str,
        access_token: str,
        shop_cipher: str | None = None,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        files: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Chamada assinada. Com `files` vai multipart (o corpo multipart não entra na assinatura)."""
        body_str = json.dumps(body, separators=(",", ":"), ensure_ascii=False) if body is not None else ""

        for attempt in range(1, self.max_retries + 1):
            params: dict[str, str] = {"app_key": self.app_key, "timestamp": str(int(time.time()))}
            if shop_cipher:
                params["shop_cipher"] = shop_cipher
            for k, v in (query or {}).items():
                if v is not None:
                    params[k] = str(v)
            params["sign"] = generate_sign(path, params, body_str, self.app_secret)

            try:
                if files is not None:
                    resp = self.http.request(
                        method, self.api_base + path, params=params, data=form, files=files,
                        headers={"x-tts-access-token": access_token},
                    )
                else:
                    resp = self.http.request(
                        method,
                        self.api_base + path,
                        params=params,
                        content=body_str.encode() if body_str else None,
                        headers={"x-tts-access-token": access_token, "content-type": "application/json"},
                    )
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise PlatformError(f"Falha de conexão com TikTok: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise TikTokAPIError(resp.status_code, resp.text[:300])
                time.sleep(2**attempt)
                continue

            data = resp.json()
            if data.get("code") != 0:
                raise TikTokAPIError(data.get("code"), data.get("message", ""), data.get("request_id"))
            return data.get("data") or {}

        raise PlatformError("Tentativas esgotadas")  # inalcançável

    # ---------- autorização ----------

    def _token_call(self, path: str, extra: dict[str, str]) -> TokenSet:
        params = {"app_key": self.app_key, "app_secret": self.app_secret, **extra}
        resp = self.http.get(self.auth_base + path, params=params)
        data = resp.json()
        if data.get("code") != 0:
            raise TikTokAPIError(data.get("code"), data.get("message", ""), data.get("request_id"))
        d = data["data"]
        return TokenSet(
            access_token=d["access_token"],
            access_token_expires_at=datetime.fromtimestamp(int(d["access_token_expire_in"]), UTC),
            refresh_token=d["refresh_token"],
            refresh_token_expires_at=datetime.fromtimestamp(int(d["refresh_token_expire_in"]), UTC),
            seller_name=d.get("seller_name"),
            region=d.get("seller_base_region"),
            granted_scopes=d.get("granted_scopes"),
        )

    def exchange_code(self, auth_code: str) -> TokenSet:
        return self._token_call(
            "/api/v2/token/get", {"auth_code": auth_code, "grant_type": "authorized_code"}
        )

    def refresh_token(self, refresh_token: str) -> TokenSet:
        return self._token_call(
            "/api/v2/token/refresh", {"refresh_token": refresh_token, "grant_type": "refresh_token"}
        )

    def get_authorized_shops(self, access_token: str) -> list[dict[str, Any]]:
        return self.request("GET", AUTHORIZED_SHOPS, access_token).get("shops", [])

    # ---------- devoluções ----------

    def search_returns(
        self,
        access_token: str,
        shop_cipher: str,
        filters: dict[str, Any],
        page_size: int = 50,
        page_token: str | None = None,
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            RETURNS_SEARCH,
            access_token,
            shop_cipher,
            query={"page_size": page_size, "page_token": page_token, "sort_field": "update_time", "sort_order": "ASC"},
            body=filters,
        )

    def approve_return(
        self, access_token: str, shop_cipher: str, return_id: str, decision: str, idempotency_key: str
    ) -> dict[str, Any]:
        return self.request(
            "POST",
            RETURN_APPROVE.format(return_id=return_id),
            access_token,
            shop_cipher,
            query={"idempotency_key": idempotency_key},
            body={"decision": decision},
        )

    def upload_image(
        self, access_token: str, filename: str, content: bytes, content_type: str, use_case: str = "DESCRIPTION_IMAGE"
    ) -> dict[str, Any]:
        """Envia uma imagem (até 10 MB) e devolve {uri, width, height}. Exige o escopo seller.product.basic.

        Docs: upload-product-image-202309; o `uri` vai em images[].image_id na recusa da devolução.
        """
        return self.request(
            "POST",
            IMAGE_UPLOAD,
            access_token,
            files={"data": (filename, content, content_type)},
            form={"use_case": use_case},
        )

    def reject_return(
        self,
        access_token: str,
        shop_cipher: str,
        return_id: str,
        decision: str,
        reject_reason: str,
        comment: str | None,
        idempotency_key: str,
        images: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {"decision": decision, "reject_reason": reject_reason}
        if comment:
            body["comment"] = comment
        if images:
            body["images"] = images
        return self.request(
            "POST",
            RETURN_REJECT.format(return_id=return_id),
            access_token,
            shop_cipher,
            query={"idempotency_key": idempotency_key},
            body=body,
        )

    def get_reject_reasons(
        self, access_token: str, shop_cipher: str, return_id: str, locale: str
    ) -> list[dict[str, Any]]:
        data = self.request(
            "GET",
            REJECT_REASONS,
            access_token,
            shop_cipher,
            query={"return_or_cancel_id": return_id, "locale": locale},
        )
        return data.get("reasons", [])

    def get_return_records(self, access_token: str, shop_cipher: str, return_id: str, locale: str) -> list[dict[str, Any]]:
        data = self.request(
            "GET", RETURN_RECORDS.format(return_id=return_id), access_token, shop_cipher, query={"locale": locale}
        )
        return data.get("records", [])
