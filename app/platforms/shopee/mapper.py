"""Converte devoluções da Shopee para o modelo normalizado."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...enums import ReturnStatus, ReturnType
from ..base import NormalizedItem, NormalizedReturn

# Logística reversa (logistics_status) que indica o pacote a caminho ou já entregue ao vendedor
_SHIPPED = {"LOGISTICS_PICKUP_DONE", "LOGISTICS_PICKUP_RETRY", "LOGISTICS_DELIVERY_FAILED"}
_DELIVERED = {"LOGISTICS_DELIVERY_DONE"}

# Com o Devolução Fácil a Shopee aceita (ACCEPTED) antes de o pacote chegar: o reembolso está
# resolvido e o acompanhamento físico segue pela conferência (received_at) e pelo rastreio.
STATUS_MAP: dict[str, ReturnStatus] = {
    "REQUESTED": ReturnStatus.PENDING_REVIEW,
    "PROCESSING": ReturnStatus.PENDING_REVIEW,
    "ACCEPTED": ReturnStatus.COMPLETED,
    "JUDGING": ReturnStatus.DISPUTED,
    "SELLER_DISPUTE": ReturnStatus.DISPUTED,
    "CLOSED": ReturnStatus.REJECTED,
    "CANCELLED": ReturnStatus.CANCELLED,
}

# Mesmos rótulos do sistema antigo, para o histórico importado e o novo agruparem juntos
REASON_LABELS = {
    "CHANGE_MIND": "Arrependimento",
    "ITEM_NOT_FIT": "Não serviu",
    "FUNCTIONAL_DMG": "Defeito de fabricação",
    "PHYSICAL_DMG": "Produto danificado",
    "DAMAGED_OTHERS": "Produto danificado",
    "ITEM_DAMAGED": "Produto danificado",
    "NOT_RECEIPT": "Não recebi o pacote",
    "WRONG_ITEM": "Produto errado",
    "DIFF_DESC": "Diferente da descrição",
    "ITEM_MISSING": "Item faltando no pacote",
    "EXPECTATION_FAILED": "Não atendeu às expectativas",
    "ITEM_FAKE": "Produto falsificado",
    "MUTUAL_AGREE": "Acordo com o vendedor",
    "OTHER": "Outro",
}


def _ts(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(value), UTC)


def _money(value: Any) -> Decimal | None:
    if value in (None, ""):
        return None
    try:
        return Decimal(str(value))
    except InvalidOperation:
        return None


def map_status(status: str | None, logistics_status: str | None, negotiation_status: str | None = None) -> ReturnStatus:
    mapped = STATUS_MAP.get(status or "", ReturnStatus.UNKNOWN)
    if mapped == ReturnStatus.PENDING_REVIEW:
        # Ainda aguardando o vendedor, mas o pacote já pode estar a caminho ou ter chegado
        if logistics_status in _DELIVERED:
            return ReturnStatus.RECEIVED
        if logistics_status in _SHIPPED:
            return ReturnStatus.IN_TRANSIT
        # PROCESSING sem negociação pendente: a loja só valida quando o pacote chegar
        if status == "PROCESSING" and negotiation_status != "PENDING_RESPOND":
            return ReturnStatus.AWAITING_BUYER_SHIPMENT
    return mapped


def map_return(r: dict[str, Any]) -> NormalizedReturn:
    status = r.get("status")
    logistics = r.get("logistics_status")

    items = []
    for i in r.get("item") or []:
        qty = int(i.get("amount") or 1)
        price = _money(i.get("item_price"))
        images = i.get("images") or []
        items.append(
            NormalizedItem(
                external_item_id=str(i.get("item_id")) if i.get("item_id") else None,
                sku_id=str(i.get("model_id")) if i.get("model_id") else None,
                # O SKU da variação é o que bate com o código do produto no Olist
                seller_sku=i.get("variation_sku") or i.get("item_sku") or None,
                product_name=i.get("name"),
                sku_name=i.get("variation_sku"),
                quantity=qty,
                refund_amount=price * qty if price is not None else None,
                image_url=images[0] if images else None,
                raw=i,
            )
        )

    reason = r.get("reason")
    return NormalizedReturn(
        external_return_id=str(r["return_sn"]),
        external_order_id=r.get("order_sn"),
        return_type=ReturnType.RETURN_AND_REFUND if r.get("needs_logistics") else ReturnType.REFUND_ONLY,
        status=map_status(status, logistics, r.get("negotiation_status")),
        platform_status=f"{status} / {logistics}" if logistics else status,
        reason_code=reason,
        # Motivo padronizado (agrupa nos relatórios); o comentário livre do comprador fica em raw.text_reason
        reason_text=REASON_LABELS.get(reason or "") or r.get("text_reason") or reason,
        refund_amount=_money(r.get("refund_amount")),
        currency=r.get("currency") or "BRL",
        requested_at=_ts(r.get("create_time")),
        platform_updated_at=_ts(r.get("update_time")),
        action_deadline=_ts(r.get("return_seller_due_date") or r.get("due_date")),
        tracking_number=r.get("tracking_number") or None,
        carrier=None,
        raw=r,
        items=items,
    )
