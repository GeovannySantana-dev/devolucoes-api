# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Cliente HTTP da Shopee Open Platform v2.

Docs: https://open.shopee.com/documents/v2/v2.returns.get_return_list
Referência: documentação da Shopee Open Platform (v2).
"""

import hashlib
import hmac
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..base import PlatformError, TokenSet

log = logging.getLogger(__name__)

AUTH_PARTNER = "/api/v2/shop/auth_partner"
TOKEN_GET = "/api/v2/auth/token/get"
TOKEN_REFRESH = "/api/v2/auth/access_token/get"
SHOP_INFO = "/api/v2/shop/get_shop_info"
RETURN_LIST = "/api/v2/returns/get_return_list"
RETURN_DETAIL = "/api/v2/returns/get_return_detail"
RETURN_CONFIRM = "/api/v2/returns/confirm"
RETURN_DISPUTE = "/api/v2/returns/dispute"
RETURN_CONVERT_IMAGE = "/api/v2/returns/convert_image"
DISPUTE_REASONS = "/api/v2/returns/get_return_dispute_reason"
REVERSE_TRACKING = "/api/v2/returns/get_reverse_tracking_info"

# get_return_list aceita no máximo 15 dias por consulta (o antigo usava 15 dias - 60 s)
MAX_WINDOW = timedelta(days=15) - timedelta(seconds=60)
PAGE_SIZE = 50
# A Shopee não informa a validade do refresh_token; a documentação diz 30 dias
REFRESH_TOKEN_TTL = timedelta(days=30)


class ShopeeAPIError(PlatformError):
    def __init__(self, error: str, message: str, request_id: str | None = None):
        self.error = error
        self.request_id = request_id
        super().__init__(f"Shopee API erro {error}: {message} (request_id={request_id})")


def sign(partner_key: str, *parts: Any) -> str:
    """HMAC-SHA256 da concatenação: partner_id + path + timestamp [+ access_token + shop_id]."""
    base = "".join(str(p) for p in parts)
    return hmac.new(partner_key.encode(), base.encode(), hashlib.sha256).hexdigest()


class ShopeeClient:
    def __init__(
        self,
        partner_id: int | None,
        partner_key: str,
        api_base: str,
        http: httpx.Client | None = None,
        max_retries: int = 3,
    ):
        self.partner_id = partner_id
        self.partner_key = partner_key
        self.api_base = api_base.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.max_retries = max_retries

    def _check_config(self) -> None:
        if not self.partner_id or not self.partner_key:
            raise PlatformError("SHOPEE_PARTNER_ID / SHOPEE_PARTNER_KEY não configurados")

    # ---------- infraestrutura ----------

    def request(
        self,
        method: str,
        path: str,
        access_token: str | None = None,
        shop_id: str | int | None = None,
        query: dict[str, Any] | None = None,
        body: dict[str, Any] | None = None,
        form: dict[str, Any] | None = None,
        files: dict[str, tuple[str, bytes, str]] | None = None,
    ) -> dict[str, Any]:
        """Chamada assinada. Com access_token/shop_id é uma chamada da loja; sem, é de autenticação.

        `form` + `files` enviam multipart (upload de imagem); senão o corpo vai em JSON.
        """
        self._check_config()
        for attempt in range(1, self.max_retries + 1):
            ts = int(time.time())
            params: dict[str, Any] = {"partner_id": self.partner_id, "timestamp": ts}
            if access_token:
                params["access_token"] = access_token
                params["shop_id"] = shop_id
                params["sign"] = sign(self.partner_key, self.partner_id, path, ts, access_token, shop_id)
            else:
                params["sign"] = sign(self.partner_key, self.partner_id, path, ts)
            for k, v in (query or {}).items():
                if v is not None:
                    params[k] = v

            try:
                if files is not None:
                    resp = self.http.request(method, self.api_base + path, params=params, data=form, files=files)
                else:
                    resp = self.http.request(method, self.api_base + path, params=params, json=body)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise PlatformError(f"Falha de conexão com Shopee: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise ShopeeAPIError(str(resp.status_code), resp.text[:300])
                time.sleep(2**attempt)
                continue

            data = resp.json()
            # Algumas respostas de sucesso trazem "error": " " (com espaço)
            if str(data.get("error") or "").strip():
                raise ShopeeAPIError(data["error"], data.get("message", ""), data.get("request_id"))
            return data

        raise PlatformError("Tentativas esgotadas")  # inalcançável

    # ---------- autorização ----------

    def authorize_url(self, redirect: str) -> str:
        """Link que o dono da loja abre para autorizar o app. A Shopee volta com ?code=&shop_id=."""
        self._check_config()
        ts = int(time.time())
        params = {
            "partner_id": self.partner_id,
            "timestamp": ts,
            "sign": sign(self.partner_key, self.partner_id, AUTH_PARTNER, ts),
            "redirect": redirect,
        }
        return f"{self.api_base}{AUTH_PARTNER}?{urlencode(params)}"

    def _token_set(self, data: dict[str, Any]) -> TokenSet:
        now = datetime.now(UTC)
        return TokenSet(
            access_token=data["access_token"],
            access_token_expires_at=now + timedelta(seconds=int(data.get("expire_in") or 14400)),
            refresh_token=data["refresh_token"],
            refresh_token_expires_at=now + REFRESH_TOKEN_TTL,
            seller_name=None,
            region=None,
        )

    def exchange_code(self, code: str, shop_id: str) -> TokenSet:
        data = self.request(
            "POST", TOKEN_GET, body={"code": code, "shop_id": int(shop_id), "partner_id": self.partner_id}
        )
        return self._token_set(data)

    def refresh_token(self, refresh_token: str, shop_id: str) -> TokenSet:
        data = self.request(
            "POST",
            TOKEN_REFRESH,
            body={"refresh_token": refresh_token, "shop_id": int(shop_id), "partner_id": self.partner_id},
        )
        return self._token_set(data)

    def get_shop_info(self, access_token: str, shop_id: str) -> dict[str, Any]:
        return self.request("GET", SHOP_INFO, access_token, shop_id)

    # ---------- devoluções ----------

    def iter_returns(
        self, access_token: str, shop_id: str, updated_since: datetime, updated_until: datetime
    ) -> Iterator[dict[str, Any]]:
        """Percorre as devoluções atualizadas no período, em janelas de até 15 dias.

        Pode repetir a mesma devolução entre janelas; quem grava descarta a versão mais antiga.
        """
        start = updated_since
        while start < updated_until:
            end = min(start + MAX_WINDOW, updated_until)
            page = 0
            while True:
                data = self.request(
                    "GET",
                    RETURN_LIST,
                    access_token,
                    shop_id,
                    query={
                        # `page_no` é o nº da página a partir de 0 (testado em 15/10/2026; a nota do
                        # sistema antigo dizia offset, mas page_no=50 volta vazio)
                        "page_no": page,
                        "page_size": PAGE_SIZE,
                        "update_time_from": int(start.timestamp()),
                        "update_time_to": int(end.timestamp()),
                    },
                )
                response = data.get("response") or {}
                rows = response.get("return") or []
                yield from rows
                if not response.get("more") or not rows:
                    break
                page += 1
            start = end

    def get_return(self, access_token: str, shop_id: str, return_sn: str) -> dict[str, Any] | None:
        data = self.request("GET", RETURN_DETAIL, access_token, shop_id, query={"return_sn": return_sn})
        return data.get("response") or None

    def confirm_return(self, access_token: str, shop_id: str, return_sn: str) -> dict[str, Any]:
        """Aceita a devolução/reembolso."""
        return self.request("POST", RETURN_CONFIRM, access_token, shop_id, body={"return_sn": return_sn})

    def convert_image(
        self, access_token: str, shop_id: str, return_sn: str, filename: str, content: bytes, content_type: str
    ) -> str:
        """Envia uma foto de evidência (JPG/PNG até 10 MB) e devolve a URL para usar na contestação."""
        data = self.request(
            "POST",
            RETURN_CONVERT_IMAGE,
            access_token,
            shop_id,
            form={"return_sn": return_sn},
            files={"upload_image": (filename, content, content_type)},
        )
        url = (data.get("response") or {}).get("url")
        if not url:
            raise ShopeeAPIError("no_url", "convert_image não devolveu a URL da imagem", data.get("request_id"))
        return url

    def dispute_return(
        self,
        access_token: str,
        shop_id: str,
        return_sn: str,
        email: str,
        reason_id: int,
        text_reason: str | None,
        image_list: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        """Contesta a devolução (equivale à recusa); a Shopee analisa o caso.

        `image_list`: [{"module_index", "requirement", "image_url": [até 3]}] para cada módulo de evidência
        exigido pelo motivo (ver get_return_dispute_reason). Docs: v2.returns.dispute (atualizada em 2025-12).
        """
        body: dict[str, Any] = {"return_sn": return_sn, "email": email, "dispute_reason_id": int(reason_id)}
        if text_reason:
            body["dispute_text_reason"] = text_reason
        if image_list:
            body["image_list"] = image_list
        return self.request("POST", RETURN_DISPUTE, access_token, shop_id, body=body)

    def get_dispute_reasons(self, access_token: str, shop_id: str, return_sn: str) -> list[dict[str, Any]]:
        data = self.request("GET", DISPUTE_REASONS, access_token, shop_id, query={"return_sn": return_sn})
        response = data.get("response") or {}
        return response.get("dispute_reason_list") or response.get("dispute_reason") or []

    def get_tracking_number(self, access_token: str, shop_id: str, order_sn: str) -> str | None:
        """Rastreio do envio da venda (a etiqueta que volta colada no pacote não entregue)."""
        data = self.request("GET", "/api/v2/logistics/get_tracking_number", access_token, shop_id,
                            query={"order_sn": order_sn})
        return ((data.get("response") or {}).get("tracking_number") or "").strip().upper() or None

    def get_reverse_tracking(self, access_token: str, shop_id: str, return_sn: str) -> dict[str, Any]:
        data = self.request("GET", REVERSE_TRACKING, access_token, shop_id, query={"return_sn": return_sn})
        return data.get("response") or {}
