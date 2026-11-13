from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING

from ..base import NormalizedReturn, PlatformAdapter, PlatformError, PlatformRejectReason
from .client import ShopeeClient
from .mapper import map_return

if TYPE_CHECKING:
    from ...models import Return, Store


MAX_IMAGES_PER_MODULE = 3  # limite da Shopee por módulo de evidência


def _required(module: dict) -> bool:
    return str(module.get("is_required")).lower() == "true"


class ShopeeAdapter(PlatformAdapter):
    def __init__(
        self,
        client: ShopeeClient,
        token_provider: Callable[["Store"], str],
        dispute_email: str,
        evidence_provider: Callable[["Return"], list[tuple[str, bytes, str]]] = lambda ret: [],
    ):
        self.client = client
        self.token_provider = token_provider
        self.dispute_email = dispute_email
        # Fotos da conferência (nome, bytes, content-type) usadas como evidência na contestação
        self.evidence_provider = evidence_provider

    def _auth(self, store: "Store") -> tuple[str, str]:
        if not store.external_shop_id:
            raise PlatformError(f"Loja {store.name} sem shop_id da Shopee")
        return self.token_provider(store), store.external_shop_id

    def fetch_returns(
        self, store: "Store", updated_since: datetime, updated_until: datetime
    ) -> Iterator[NormalizedReturn]:
        token, shop_id = self._auth(store)
        for r in self.client.iter_returns(token, shop_id, updated_since, updated_until):
            yield map_return(r)

    def fetch_return(self, store: "Store", external_return_id: str) -> NormalizedReturn | None:
        token, shop_id = self._auth(store)
        r = self.client.get_return(token, shop_id, external_return_id)
        return map_return(r) if r else None

    def approve(self, store: "Store", ret: "Return", decision: str | None = None) -> None:
        token, shop_id = self._auth(store)
        self.client.confirm_return(token, shop_id, ret.external_return_id)

    def reject(
        self,
        store: "Store",
        ret: "Return",
        platform_reason_code: str,
        comment: str | None,
        decision: str | None = None,
    ) -> None:
        """Contesta na Shopee anexando as fotos da conferência em cada módulo de evidência exigido."""
        if not self.dispute_email:
            raise PlatformError("Configure SHOPEE_DISPUTE_EMAIL para contestar devoluções na Shopee")
        token, shop_id = self._auth(store)
        reasons = self.client.get_dispute_reasons(token, shop_id, ret.external_return_id)
        chosen = next((r for r in reasons if str(r.get("dispute_reason")) == str(platform_reason_code)), None)
        if chosen is None:
            raise PlatformError(f"Motivo {platform_reason_code} não está disponível para esta devolução na Shopee")
        required = [m for m in chosen.get("evidence_module_list") or [] if _required(m)]

        image_list = []
        if required:
            photos = self.evidence_provider(ret)
            if not photos:
                raise PlatformError(
                    "A Shopee exige fotos para este motivo: adicione fotos da conferência (JPG ou PNG, até 10 MB) "
                    "antes de contestar. Vídeos não são aceitos por essa via."
                )
            # Cada foto é enviada uma vez e a mesma URL serve para todos os módulos exigidos (máx. 3 por módulo)
            urls = [
                self.client.convert_image(token, shop_id, ret.external_return_id, name, content, ctype)
                for name, content, ctype in photos[:MAX_IMAGES_PER_MODULE]
            ]
            image_list = [
                {"module_index": int(m["module_index"]), "requirement": m.get("requirement") or "", "image_url": urls}
                for m in required
            ]

        self.client.dispute_return(
            token, shop_id, ret.external_return_id, self.dispute_email, int(platform_reason_code), comment, image_list
        )

    def reject_reasons(self, store: "Store", ret: "Return") -> list[PlatformRejectReason]:
        """Motivos de contestação. Formato real (testado em 16/10/2026):
        {"dispute_reason": 84, "dispute_requirement": "...", "evidence_module_list": [{"requirement", "is_required"}]}
        """
        token, shop_id = self._auth(store)
        out = []
        for r in self.client.get_dispute_reasons(token, shop_id, ret.external_return_id):
            code = r.get("dispute_reason")
            if code is None:
                continue
            modules = r.get("evidence_module_list") or []
            # O texto do módulo de evidência descreve o motivo ("...item retornado é diferente do anúncio")
            label = next((m.get("requirement") for m in modules if m.get("requirement")), None)
            label = " ".join((label or r.get("dispute_requirement") or f"Motivo {code}").split())
            if any(_required(m) for m in modules):
                label += " (envia as fotos da conferência)"
            out.append(PlatformRejectReason(code=str(code), label=label))
        return out
