# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from collections.abc import Callable, Iterator
from datetime import datetime
from typing import TYPE_CHECKING, Any

from ..base import NormalizedReturn, PlatformAdapter, PlatformError, PlatformRejectReason
from .client import MercadoLivreAPIError, MercadoLivreClient
from .mapper import IGNORED_TYPES, map_claim, respondent_actions

if TYPE_CHECKING:
    from ...models import Return, Store

# Decisões aceitas em `decision` (aprovar), na ordem em que são escolhidas automaticamente:
# a devolução chegou e está certa > reembolsar > aceitar a devolução (gera etiqueta para o comprador)
APPROVE_ACTIONS = ("return_review_ok", "refund", "allow_return")
OPEN_DISPUTE = "open_dispute"
# Motivos de revisão com falha que o Mercado Livre só aceita com fotos anexadas
NEEDS_ATTACHMENTS = {"SRF2", "SRF4"}


MAX_ATTACHMENTS = 3


def _may_have_change(claim: dict[str, Any]) -> bool:
    """A busca de reclamações não traz `related_entities`: aí vale a troca resolvida ou qualquer reclamação aberta."""
    if "change" in (claim.get("related_entities") or []):
        return True
    if (claim.get("resolution") or {}).get("reason") == "item_changed":
        return True
    return "related_entities" not in claim and claim.get("status") == "opened"


class MercadoLivreAdapter(PlatformAdapter):
    def __init__(
        self,
        client: MercadoLivreClient,
        token_provider: Callable[["Store"], str],
        evidence_provider: Callable[["Return"], list[tuple[str, bytes, str]]] = lambda ret: [],
    ):
        self.client = client
        self.token_provider = token_provider
        # Fotos da conferência (nome, bytes, content-type) anexadas na revisão com problema
        self.evidence_provider = evidence_provider
        self._reasons: dict[str, dict[str, Any]] = {}
        self._skus: dict[tuple[str, str], str | None] = {}

    def _auth(self, store: "Store") -> tuple[str, str]:
        if not store.external_shop_id:
            raise PlatformError(f"Loja {store.name} sem user_id do Mercado Livre")
        return self.token_provider(store), store.external_shop_id

    def _reason(self, token: str, reason_id: str | None) -> dict[str, Any] | None:
        if not reason_id:
            return None
        if reason_id not in self._reasons:
            try:
                self._reasons[reason_id] = self.client.get_reason(token, reason_id)
            except MercadoLivreAPIError:
                self._reasons[reason_id] = {}
        return self._reasons[reason_id]

    def _sku_lookup(self, token: str) -> Callable[[str, Any], str | None]:
        def lookup(item_id: str, variation_id: Any) -> str | None:
            key = (str(item_id), str(variation_id))
            if key not in self._skus:
                try:
                    self._skus[key] = self.client.get_variation_sku(token, item_id, variation_id)
                except MercadoLivreAPIError:
                    self._skus[key] = None
            return self._skus[key]

        return lookup

    def _order_id(self, token: str, claim: dict[str, Any], ret: dict[str, Any] | None) -> str | None:
        if claim.get("resource") == "order":
            return str(claim["resource_id"])
        for o in (ret or {}).get("orders") or []:
            if o.get("order_id"):
                return str(o["order_id"])
        if claim.get("resource") == "shipment":
            return str(self.client.get_shipment(token, str(claim["resource_id"])).get("order_id") or "") or None
        return None

    def _build(self, token: str, claim: dict[str, Any]) -> NormalizedReturn:
        claim_id = str(claim["id"])
        ret = self.client.get_claim_return(token, claim_id)
        order_id = self._order_id(token, claim, ret)
        order = self.client.get_order(token, order_id) if order_id else None
        cost = self.client.get_return_cost(token, claim_id) if ret else None
        nr = map_claim(
            claim, ret, order, self._reason(token, claim.get("reason_id")), self._sku_lookup(token), cost
        )
        if _may_have_change(claim):
            # Troca: o ML mostra a revisão no pedido novo (outro nº e outro pacote). Guardar esses números faz a
            # busca do app achar a devolução por qualquer um deles.
            nr.raw["change"] = self._change_numbers(token, claim_id)
        return nr

    def _change_numbers(self, token: str, claim_id: str) -> dict[str, Any]:
        orders, packs, shipments, status = [], [], [], None
        for change in self.client.get_claim_changes(token, claim_id):
            status = change.get("status") or status
            shipments += [str(s["id"]) for s in change.get("new_orders_shipments") or [] if s.get("id")]
            for o in change.get("new_orders_ids") or []:
                if not o.get("id"):
                    continue
                orders.append(str(o["id"]))
                try:
                    pack = self.client.get_order(token, str(o["id"])).get("pack_id")
                except MercadoLivreAPIError:
                    pack = None
                if pack:
                    packs.append(str(pack))
        return {"orders": orders, "packs": packs, "shipments": shipments, "status": status}

    def fetch_returns(
        self, store: "Store", updated_since: datetime, updated_until: datetime
    ) -> Iterator[NormalizedReturn]:
        token, user_id = self._auth(store)
        for claim in self.client.iter_claims(token, user_id, updated_since):
            if claim.get("type") in IGNORED_TYPES:
                continue
            yield self._build(token, claim)

    def fetch_return(self, store: "Store", external_return_id: str) -> NormalizedReturn | None:
        token, _ = self._auth(store)
        claim = self.client.get_claim(token, external_return_id)
        return self._build(token, claim) if claim else None

    # ---------- ações ----------

    def _current_actions(self, token: str, ret: "Return") -> set[str]:
        """Ações disponíveis agora (consulta a reclamação de novo: o que está gravado pode estar velho)."""
        claim = self.client.get_claim(token, ret.external_return_id)
        if not claim:
            raise PlatformError("Reclamação não encontrada no Mercado Livre")
        return {a.get("action") for a in respondent_actions(claim)}

    def _return_id(self, token: str, ret: "Return") -> str:
        return_id = ((ret.raw or {}).get("return") or {}).get("id")
        if not return_id:
            fresh = self.client.get_claim_return(token, ret.external_return_id)
            return_id = (fresh or {}).get("id")
        if not return_id:
            raise PlatformError("Reclamação sem devolução física associada")
        return str(return_id)

    def approve(self, store: "Store", ret: "Return", decision: str | None = None) -> None:
        token, _ = self._auth(store)
        available = self._current_actions(token, ret)
        if decision:
            if decision not in APPROVE_ACTIONS:
                raise PlatformError(f"decision deve ser uma de {list(APPROVE_ACTIONS)}")
            action = decision
        else:
            action = next((a for a in APPROVE_ACTIONS if a in available), None)
        if not action or action not in available:
            raise PlatformError(
                f"Nenhuma ação de aprovação disponível agora no Mercado Livre (disponíveis: {sorted(available)})"
            )

        if action == "return_review_ok":
            self.client.return_review(token, self._return_id(token, ret))
        elif action == "refund":
            self.client.refund(token, ret.external_return_id)
        else:
            self.client.allow_return(token, ret.external_return_id)

    def reject(
        self,
        store: "Store",
        ret: "Return",
        platform_reason_code: str,
        comment: str | None,
        decision: str | None = None,
    ) -> None:
        token, _ = self._auth(store)
        available = self._current_actions(token, ret)

        if platform_reason_code == OPEN_DISPUTE:
            if OPEN_DISPUTE not in available:
                raise PlatformError("Abrir mediação não está disponível para esta reclamação agora")
            self.client.open_dispute(token, ret.external_return_id)
            return

        # Demais motivos: revisão da devolução recebida com problema (SRF*)
        if "return_review_fail" not in available:
            raise PlatformError("Revisão da devolução não está disponível agora (o pacote ainda não chegou?)")
        if not comment:
            raise PlatformError("Informe o comentário explicando o problema com o produto devolvido")
        photos = self.evidence_provider(ret)[:MAX_ATTACHMENTS]
        if platform_reason_code in NEEDS_ATTACHMENTS and not photos:
            raise PlatformError(
                "Para este motivo o Mercado Livre exige fotos: adicione fotos da conferência (JPG ou PNG) antes de recusar"
            )
        # As fotos vão como evidência (obrigatórias no SRF2/SRF4, opcionais nos demais)
        attachments = [
            self.client.upload_return_attachment(token, ret.external_return_id, name, content, ctype)
            for name, content, ctype in photos
        ]
        review: dict[str, Any] = {"reason": platform_reason_code, "message": comment}
        if attachments:
            review["attachments"] = attachments
        self.client.return_review(token, self._return_id(token, ret), [review])

    def reject_reasons(self, store: "Store", ret: "Return") -> list[PlatformRejectReason]:
        token, _ = self._auth(store)
        available = self._current_actions(token, ret)
        reasons = []
        if "return_review_fail" in available:
            for r in self.client.get_return_fail_reasons(token, ret.external_return_id):
                label = r.get("detail") or r.get("name") or r["id"]
                if r["id"] in NEEDS_ATTACHMENTS:
                    label += " (envia as fotos da conferência)"
                reasons.append(PlatformRejectReason(code=r["id"], label=label))
        if OPEN_DISPUTE in available:
            reasons.append(PlatformRejectReason(code=OPEN_DISPUTE, label="Abrir mediação no Mercado Livre"))
        return reasons
