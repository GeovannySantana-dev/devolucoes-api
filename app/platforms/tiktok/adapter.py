# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from ..base import NormalizedReturn, PlatformAdapter, PlatformError, PlatformRejectReason
from .client import SCOPE_DENIED, TikTokAPIError, TikTokClient
from .mapper import approve_decision, map_return, reject_decision

if TYPE_CHECKING:
    from ...models import Return, Store


MAX_IMAGES = 3
SCOPE_HELP = (
    "Recusa enviada ao TikTok sem as fotos: o app precisa da permissão de produtos (seller.product.basic) "
    "para anexar imagens. Inclua no Partner Center e autorize a loja de novo; se precisar, anexe as fotos pelo painel do TikTok."
)


class TikTokAdapter(PlatformAdapter):
    def __init__(
        self,
        client: TikTokClient,
        token_provider: Callable[["Store"], str],
        locale: str,
        evidence_provider: Callable[["Return"], list[tuple[str, bytes, str]]] = lambda ret: [],
    ):
        self.client = client
        self.token_provider = token_provider
        self.locale = locale
        # Fotos da conferência (nome, bytes, content-type) enviadas como evidência na recusa
        self.evidence_provider = evidence_provider

    def _auth(self, store: "Store") -> tuple[str, str]:
        if not store.shop_cipher:
            raise PlatformError(f"Loja {store.name} sem shop_cipher; refaça a autorização")
        return self.token_provider(store), store.shop_cipher

    def fetch_returns(
        self, store: "Store", updated_since: datetime, updated_until: datetime
    ) -> Iterator[NormalizedReturn]:
        token, cipher = self._auth(store)
        filters = {
            "update_time_ge": int(updated_since.timestamp()),
            "update_time_lt": int(updated_until.timestamp()),
        }
        page_token = None
        while True:
            data = self.client.search_returns(token, cipher, filters, page_token=page_token)
            for r in data.get("return_orders") or []:
                yield map_return(r)
            page_token = data.get("next_page_token")
            if not page_token:
                break

    def fetch_return(self, store: "Store", external_return_id: str) -> NormalizedReturn | None:
        token, cipher = self._auth(store)
        data = self.client.search_returns(token, cipher, {"return_ids": [external_return_id]}, page_size=1)
        orders = data.get("return_orders") or []
        return map_return(orders[0]) if orders else None

    def approve(self, store: "Store", ret: "Return", decision: str | None = None) -> None:
        token, cipher = self._auth(store)
        decision = decision or approve_decision(ret.return_type, ret.status)
        self.client.approve_return(
            token, cipher, ret.external_return_id, decision, idempotency_key=f"approve-{ret.id}-{ret.status}"
        )

    def reject(
        self,
        store: "Store",
        ret: "Return",
        platform_reason_code: str,
        comment: str | None,
        decision: str | None = None,
    ) -> None:
        token, cipher = self._auth(store)
        decision = decision or reject_decision(ret.return_type, ret.status)
        images = []
        self.last_warning = None
        for name, content, ctype in self.evidence_provider(ret)[:MAX_IMAGES]:
            try:
                up = self.client.upload_image(token, name, content, ctype)
            except TikTokAPIError as exc:
                if str(exc.code) != str(SCOPE_DENIED):
                    raise
                # Sem a permissão de imagens: não perde o prazo, recusa só com o comentário e avisa
                self.last_warning = SCOPE_HELP
                images = []
                break
            images.append({"image_id": up["uri"], "mime_type": ctype, "width": up.get("width"), "height": up.get("height")})
        self.client.reject_return(
            token,
            cipher,
            ret.external_return_id,
            decision,
            platform_reason_code,
            comment,
            idempotency_key=f"reject-{ret.id}-{ret.status}",
            images=images or None,
        )

    def reject_reasons(self, store: "Store", ret: "Return") -> list[PlatformRejectReason]:
        token, cipher = self._auth(store)
        reasons = self.client.get_reject_reasons(token, cipher, ret.external_return_id, self.locale)
        return [PlatformRejectReason(code=r["name"], label=r.get("text") or r["name"]) for r in reasons]
