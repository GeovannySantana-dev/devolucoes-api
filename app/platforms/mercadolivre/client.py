"""Cliente HTTP da API do Mercado Livre.

Docs: https://developers.mercadolivre.com.br/pt_br/gerenciar-reclamacoes
"""

import base64
import hashlib
import logging
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import httpx

from ..base import PlatformError, TokenSet

log = logging.getLogger(__name__)

# O refresh_token vale 6 meses e é trocado a cada renovação (o anterior deixa de valer)
REFRESH_TOKEN_TTL = timedelta(days=180)
PAGE_SIZE = 50


class MercadoLivreAPIError(PlatformError):
    def __init__(self, status: int, error: str, message: str):
        self.status = status
        self.error = error
        super().__init__(f"Mercado Livre HTTP {status} {error}: {message}")


def pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode()).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode()


class MercadoLivreClient:
    def __init__(
        self,
        client_id: str,
        client_secret: str,
        api_base: str,
        auth_url: str,
        http: httpx.Client | None = None,
        max_retries: int = 3,
        min_interval: float = 0.35,
    ):
        self.client_id = client_id
        self.client_secret = client_secret
        self.api_base = api_base.rstrip("/")
        self.auth_url = auth_url
        self.http = http or httpx.Client(timeout=30)
        self.max_retries = max_retries
        self.min_interval = min_interval
        self._last_call = 0.0

    def _check_config(self) -> None:
        if not self.client_id or not self.client_secret:
            raise PlatformError("MERCADOLIVRE_CLIENT_ID / MERCADOLIVRE_CLIENT_SECRET não configurados")

    # ---------- infraestrutura ----------

    def request(
        self, method: str, path: str, access_token: str | None = None, params: dict[str, Any] | None = None,
        json: Any = None, data: dict[str, Any] | None = None, files: dict[str, Any] | None = None,
    ) -> Any:
        self._check_config()
        headers = {"Authorization": f"Bearer {access_token}"} if access_token else {}
        attempt = 0
        while True:
            attempt += 1
            # O Mercado Livre devolve 429 com rajadas de chamadas: mantém um intervalo mínimo
            wait = self._last_call + self.min_interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.monotonic()
            try:
                resp = self.http.request(
                    method, self.api_base + path, params=params, json=json, data=data, files=files, headers=headers
                )
            except httpx.TransportError as exc:
                if attempt >= self.max_retries:
                    raise PlatformError(f"Falha de conexão com Mercado Livre: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429:
                # Limite de chamadas: espera mais e tenta mais vezes que nos erros de servidor
                if attempt >= self.max_retries + 3:
                    raise MercadoLivreAPIError(429, "too_many_requests", resp.text[:300])
                retry_after = resp.headers.get("retry-after")
                time.sleep(float(retry_after) if retry_after and retry_after.isdigit() else 5 * attempt)
                continue
            if resp.status_code >= 500:
                if attempt >= self.max_retries:
                    raise MercadoLivreAPIError(resp.status_code, "retry_exhausted", resp.text[:300])
                time.sleep(2**attempt)
                continue
            if resp.status_code >= 400:
                try:
                    body = resp.json()
                except ValueError:
                    body = {}
                raise MercadoLivreAPIError(
                    resp.status_code, str(body.get("error") or ""), str(body.get("message") or resp.text[:300])
                )
            return resp.json() if resp.content else {}

        raise PlatformError("Tentativas esgotadas")  # inalcançável

    # ---------- autorização (OAuth 2.0 com PKCE) ----------

    def authorize_url(self, redirect_uri: str, state: str, code_verifier: str) -> str:
        self._check_config()
        params = {
            "response_type": "code",
            "client_id": self.client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": pkce_challenge(code_verifier),
            "code_challenge_method": "S256",
        }
        return f"{self.auth_url}?{urlencode(params)}"

    def _token_set(self, d: dict[str, Any]) -> tuple[TokenSet, str]:
        now = datetime.now(UTC)
        ts = TokenSet(
            access_token=d["access_token"],
            access_token_expires_at=now + timedelta(seconds=int(d.get("expires_in") or 21600)),
            refresh_token=d["refresh_token"],
            refresh_token_expires_at=now + REFRESH_TOKEN_TTL,
            seller_name=None,
            region=None,
            granted_scopes=(d.get("scope") or "").split() or None,
        )
        return ts, str(d.get("user_id") or "")

    def exchange_code(self, code: str, redirect_uri: str, code_verifier: str) -> tuple[TokenSet, str]:
        """Troca o code por tokens. Retorna (tokens, user_id do vendedor)."""
        data = self.request(
            "POST",
            "/oauth/token",
            data={
                "grant_type": "authorization_code",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "code": code,
                "redirect_uri": redirect_uri,
                "code_verifier": code_verifier,
            },
        )
        return self._token_set(data)

    def refresh_token(self, refresh_token: str) -> TokenSet:
        data = self.request(
            "POST",
            "/oauth/token",
            data={
                "grant_type": "refresh_token",
                "client_id": self.client_id,
                "client_secret": self.client_secret,
                "refresh_token": refresh_token,
            },
        )
        return self._token_set(data)[0]

    def get_me(self, access_token: str) -> dict[str, Any]:
        return self.request("GET", "/users/me", access_token)

    # ---------- reclamações e devoluções ----------

    def iter_claims(self, access_token: str, user_id: str, updated_since: datetime) -> Iterator[dict[str, Any]]:
        """Reclamações em que a loja é a parte reclamada, da mais recente para a mais antiga."""
        offset = 0
        while True:
            data = self.request(
                "GET",
                "/post-purchase/v1/claims/search",
                access_token,
                params={
                    "player_role": "respondent",
                    "player_user_id": user_id,
                    "sort": "last_updated:desc",
                    "limit": PAGE_SIZE,
                    "offset": offset,
                },
            )
            rows = data.get("data") or []
            for claim in rows:
                if claim.get("last_updated") and datetime.fromisoformat(claim["last_updated"]) < updated_since:
                    return
                yield claim
            offset += PAGE_SIZE
            if not rows or offset >= int((data.get("paging") or {}).get("total") or 0):
                return

    def get_claim(self, access_token: str, claim_id: str) -> dict[str, Any] | None:
        try:
            return self.request("GET", f"/post-purchase/v1/claims/{claim_id}", access_token)
        except MercadoLivreAPIError as exc:
            if exc.status == 404:
                return None
            raise

    def get_claim_return(self, access_token: str, claim_id: str) -> dict[str, Any] | None:
        """Devolução física da reclamação; None quando não existe (ex.: produto não recebido)."""
        try:
            return self.request("GET", f"/post-purchase/v2/claims/{claim_id}/returns", access_token)
        except MercadoLivreAPIError as exc:
            if exc.status == 404:
                return None
            raise

    def get_claim_changes(self, access_token: str, claim_id: str) -> list[dict[str, Any]]:
        """Trocas da reclamação: o pedido novo (com outro nº e outro pacote) que o ML gerou para a cliente."""
        try:
            data = self.request("GET", f"/post-purchase/v1/claims/{claim_id}/changes", access_token)
        except MercadoLivreAPIError as exc:
            if exc.status in (400, 404):
                return []
            raise
        return (data or {}).get("data") or []

    def get_reason(self, access_token: str, reason_id: str) -> dict[str, Any]:
        return self.request("GET", f"/post-purchase/v1/claims/reasons/{reason_id}", access_token)

    def get_order(self, access_token: str, order_id: str) -> dict[str, Any]:
        return self.request("GET", f"/orders/{order_id}", access_token)

    def get_shipment(self, access_token: str, shipment_id: str) -> dict[str, Any]:
        return self.request("GET", f"/shipments/{shipment_id}", access_token)

    def get_return_cost(self, access_token: str, claim_id: str) -> dict[str, Any] | None:
        """Frete de devolução cobrado da loja ({"currency_id", "amount"})."""
        try:
            return self.request("GET", f"/post-purchase/v1/claims/{claim_id}/charges/return-cost", access_token)
        except MercadoLivreAPIError as exc:
            if exc.status in (400, 404):
                return None
            raise

    # ---------- ações da loja ----------
    # Docs: /devsite/manage-claim-resolutions e /devsite/manage-returns

    def refund(self, access_token: str, claim_id: str) -> Any:
        """Reembolso total ao comprador (ação `refund`)."""
        return self.request("POST", f"/marketplace/v2/claims/{claim_id}/expected-resolutions/refund", access_token)

    def allow_return(self, access_token: str, claim_id: str) -> Any:
        """Aceita a devolução: gera a etiqueta para o comprador enviar (ação `allow_return`)."""
        return self.request(
            "POST", f"/marketplace/v2/claims/{claim_id}/expected-resolutions/allow-return", access_token
        )

    def open_dispute(self, access_token: str, claim_id: str) -> Any:
        """Pede a mediação do Mercado Livre (ação `open_dispute`)."""
        return self.request("POST", f"/marketplace/v2/claims/{claim_id}/actions/open-dispute", access_token)

    def return_review(self, access_token: str, return_id: str, failures: list[dict[str, Any]] | None = None) -> Any:
        """Revisão da devolução recebida: sem `failures` = chegou como esperado (OK).

        A revisão OK vai como lista vazia `[]`: o ML recusa o `{}` que a documentação mostra (testado em 27/10/2026).
        """
        return self.request(
            "POST", f"/post-purchase/v1/returns/{return_id}/return-review", access_token, json=failures or []
        )

    def upload_return_attachment(
        self, access_token: str, claim_id: str, filename: str, content: bytes, content_type: str
    ) -> str:
        """Envia uma foto de evidência da revisão; devolve o file_name para usar em `attachments`."""
        data = self.request(
            "POST",
            f"/post-purchase/v1/claims/{claim_id}/returns/attachments",
            access_token,
            files={"file": (filename, content, content_type)},
        )
        name = (data or {}).get("file_name")
        if not name:
            raise MercadoLivreAPIError(200, "no_file_name", "o Mercado Livre não devolveu o nome do anexo")
        return name

    def get_return_fail_reasons(self, access_token: str, claim_id: str) -> list[dict[str, Any]]:
        return self.request(
            "GET",
            "/post-purchase/v1/returns/reasons",
            access_token,
            params={"flow": "seller_return_failed", "claim_id": claim_id},
        )

    def get_variation_sku(self, access_token: str, item_id: str, variation_id: Any) -> str | None:
        """SKU cadastrado na variação do anúncio (pedidos antigos não trazem o seller_sku)."""
        path = f"/items/{item_id}/variations/{variation_id}" if variation_id else f"/items/{item_id}"
        data = self.request("GET", path, access_token, params={"include_attributes": "all"})
        for attr in data.get("attributes") or []:
            if attr.get("id") == "SELLER_SKU" and attr.get("value_name"):
                return attr["value_name"]
        return data.get("seller_custom_field") or None
