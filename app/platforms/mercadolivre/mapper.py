"""Converte reclamações (claims) do Mercado Livre para o modelo normalizado.

Cada devolução é uma reclamação (`/post-purchase/v1/claims`) com, opcionalmente, uma devolução física
(`/post-purchase/v2/claims/{id}/returns`) e o pedido (`/orders/{id}`).
"""

from collections.abc import Callable
from datetime import datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...enums import ReturnStatus, ReturnType
from ..base import NormalizedItem, NormalizedReturn

# Cancelamento antes do envio: não é devolução
IGNORED_TYPES = {"cancel_purchase"}

# Rótulos no mesmo padrão da Shopee (e que casam com as regras de revenda/defeito)
REASON_LABELS = {
    "repentant_buyer": "Arrependimento",
    "undelivered_repentant_buyer": "Arrependimento (antes da entrega)",
    "different_color_or_size_fashion": "Cor, tamanho ou modelo diferente do escolhido",
    "not_match_size_guide_fashion": "Medidas não batem com o guia de tamanhos",
    "bigger_than_expected_fashion": "Tamanho ficou grande",
    "smaller_than_expected_fashion": "Tamanho ficou pequeno",
    "broken_item": "Produto com defeito",
    "broken_item_fashion": "Produto com defeito",
    "different_than_published": "Diferente do anúncio",
    "different_item_other": "Produto errado",
    "delivered_but_not_receive_package": "Não recebi o pacote",
    "estimated_delivery_out_of_time": "Atraso na entrega",
    "undelivered_other": "Outro motivo",
}

_RETURN_STATUS = {
    "pending": ReturnStatus.AWAITING_BUYER_SHIPMENT,
    "label_generated": ReturnStatus.AWAITING_BUYER_SHIPMENT,
    "ready_to_ship": ReturnStatus.AWAITING_BUYER_SHIPMENT,
    "shipped": ReturnStatus.IN_TRANSIT,
    "delivered": ReturnStatus.RECEIVED,
}


def _dt(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value) if value else None


def _dec(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def respondent_actions(claim: dict[str, Any]) -> list[dict[str, Any]]:
    return [
        a
        for p in claim.get("players") or []
        if p.get("role") == "respondent"
        for a in p.get("available_actions") or []
    ]


REVIEW_ACTIONS = {"return_review_ok", "return_review_fail", "return_review_unified_ok", "return_review_unified_fail"}


def map_status(claim: dict[str, Any], ret: dict[str, Any] | None) -> ReturnStatus:
    # O ML reembolsa e fecha a reclamação antes de a loja revisar a peça que voltou: enquanto a revisão
    # ("Já revisei") estiver aberta, a devolução continua pendente para a loja, com o prazo da revisão
    if claim.get("status") == "closed" and REVIEW_ACTIONS & {a.get("action") for a in respondent_actions(claim)}:
        return ReturnStatus.RECEIVED if (ret or {}).get("status") == "delivered" else ReturnStatus.IN_TRANSIT
    if claim.get("status") == "closed":
        benefited = (claim.get("resolution") or {}).get("benefited") or []
        if "complainant" in benefited:
            return ReturnStatus.COMPLETED
        if "respondent" in benefited:
            return ReturnStatus.REJECTED
        return ReturnStatus.CANCELLED
    if claim.get("stage") == "dispute":
        return ReturnStatus.DISPUTED  # mediação do Mercado Livre
    if ret and ret.get("status") in _RETURN_STATUS:
        return _RETURN_STATUS[ret["status"]]
    # Aberta e sem devolução física em andamento: depende de a loja ter ação a tomar
    return ReturnStatus.PENDING_REVIEW if respondent_actions(claim) else ReturnStatus.DISPUTED


def _deadline(claim: dict[str, Any]) -> datetime | None:
    dates = [_dt(a["due_date"]) for a in respondent_actions(claim) if a.get("due_date")]
    return min(dates) if dates else None


def map_claim(
    claim: dict[str, Any],
    ret: dict[str, Any] | None,
    order: dict[str, Any] | None,
    reason: dict[str, Any] | None,
    sku_lookup: Callable[[str, Any], str | None] = lambda item_id, variation_id: None,
    return_cost: dict[str, Any] | None = None,
) -> NormalizedReturn:
    order = order or {}
    order_items = order.get("order_items") or []

    # Itens devolvidos: a devolução física diz quais e quantos; sem ela, o pedido inteiro
    returned = {
        (str(o.get("item_id")), str(o.get("variation_id"))): o
        for o in (ret or {}).get("orders") or []
    }
    items = []
    for oi in order_items:
        item = oi.get("item") or {}
        key = (str(item.get("id")), str(item.get("variation_id")))
        if returned and key not in returned:
            continue
        qty = int(float((returned.get(key) or {}).get("return_quantity") or oi.get("quantity") or 1))
        price = _dec(oi.get("unit_price"))
        attrs = item.get("variation_attributes") or []
        items.append(
            NormalizedItem(
                external_item_id=item.get("id"),
                sku_id=str(item["variation_id"]) if item.get("variation_id") else None,
                seller_sku=item.get("seller_sku")
                or item.get("seller_custom_field")
                or sku_lookup(item.get("id"), item.get("variation_id")),
                product_name=item.get("title"),
                sku_name=" / ".join(a["value_name"] for a in attrs if a.get("value_name")) or None,
                quantity=qty,
                refund_amount=price * qty if price is not None else None,
                image_url=None,
                raw=oi,
            )
        )

    reason = reason or {}
    resolution = (claim.get("resolution") or {}).get("reason")
    if resolution == "item_changed":
        return_type = ReturnType.REPLACEMENT
    elif ret or claim.get("type") == "returns":
        return_type = ReturnType.RETURN_AND_REFUND
    else:
        return_type = ReturnType.REFUND_ONLY

    shipments = [s for s in (ret or {}).get("shipments") or [] if s.get("type") in (None, "return")]
    platform_status = f"{claim.get('type')}:{claim.get('stage')}:{claim.get('status')}"
    if ret:
        platform_status += f" / return:{ret.get('status')}"

    refund = sum((i.refund_amount for i in items if i.refund_amount is not None), Decimal(0))
    return NormalizedReturn(
        external_return_id=str(claim["id"]),
        external_order_id=str(order["id"]) if order.get("id") else None,
        return_type=return_type,
        status=map_status(claim, ret),
        platform_status=platform_status,
        reason_code=claim.get("reason_id"),
        reason_text=REASON_LABELS.get(reason.get("name") or "") or reason.get("detail") or claim.get("reason_id"),
        refund_amount=refund if items else _dec(order.get("total_amount")),
        currency=order.get("currency_id") or "BRL",
        requested_at=_dt(claim.get("date_created")),
        platform_updated_at=_dt(claim.get("last_updated")),
        action_deadline=_deadline(claim),
        tracking_number=shipments[0].get("tracking_number") if shipments else None,
        carrier=None,
        raw={
            "claim": claim,
            "return": ret,
            "reason": reason or None,
            "return_cost": return_cost,  # frete de devolução cobrado da loja
            # Só o necessário do pedido (sem dados pessoais do comprador)
            "order": {k: order.get(k) for k in ("id", "pack_id", "status", "total_amount", "currency_id", "date_created")},
        },
        items=items,
    )
