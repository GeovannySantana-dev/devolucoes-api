# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Converte objetos de devolução do TikTok Shop para o modelo normalizado."""

from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation
from typing import Any

from ...enums import ReturnStatus, ReturnType
from ..base import NormalizedItem, NormalizedReturn

STATUS_MAP: dict[str, ReturnStatus] = {
    "RETURN_OR_REFUND_REQUEST_PENDING": ReturnStatus.PENDING_REVIEW,
    "REPLACEMENT_REQUEST_PENDING": ReturnStatus.PENDING_REVIEW,
    "AWAITING_BUYER_SHIP": ReturnStatus.AWAITING_BUYER_SHIPMENT,
    "BUYER_SHIPPED_ITEM": ReturnStatus.IN_TRANSIT,
    "RETURN_OR_REFUND_REQUEST_SUCCESS": ReturnStatus.COMPLETED,
    "RETURN_OR_REFUND_REQUEST_COMPLETE": ReturnStatus.COMPLETED,
    "REPLACEMENT_REQUEST_COMPLETE": ReturnStatus.COMPLETED,
    "REFUND_OR_RETURN_REQUEST_REJECT": ReturnStatus.REJECTED,
    "REJECT_RECEIVE_PACKAGE": ReturnStatus.REJECTED,
    "REPLACEMENT_REQUEST_REJECT": ReturnStatus.REJECTED,
    "RETURN_OR_REFUND_REQUEST_CANCEL": ReturnStatus.CANCELLED,
    "REPLACEMENT_REQUEST_CANCEL": ReturnStatus.CANCELLED,
}

# O TikTok devolve o motivo em inglês mesmo com locale pt-BR. Rótulos no mesmo padrão da Shopee e do
# Mercado Livre (agrupam juntos nos relatórios); o código original continua em reason_code.
REASON_LABELS: dict[str, str] = {
    "buyer_return_and_refund_item_doesn't_fit": "Não serviu",
    "ecom_order_delivered_refund_and_return_reason_not_match_description": "Diferente da descrição",
    "buyer_return_and_refund_fabric,_material_or_style_not_as_expected": "Tecido, material ou estilo diferente do esperado",
    "ecom_order_delivered_refund_reason_defective": "Produto com defeito",
    "buyer_return_and_refund_color_or_pattern_not_as_expected": "Cor ou estampa diferente do esperado",
    "ecom_order_delivered_refund_and_return_reason_no_need": "Arrependimento",
    "ecom_order_delivered_refund_reason_wrong_product": "Produto errado",
    "system_refund_sample_buy_now_refund_later": "Reembolso de amostra (TikTok)",
    "ecom_order_delivered_refund_reason_not_received": "Não recebi o pacote",
    "ecom_order_delivered_refund_and_return_reason_otherstypedamaged_latam": "Produto danificado",
    "ecom_order_delivered_refund_and_return_reason_missing_product": "Item faltando no pacote",
    "buyer_return_and_refund_item_arrived_too_late": "Atraso na entrega",
    "ecom_order_delivered_refund_and_return_reason_damaged": "Embalagem danificada",
    "system_cancel_order_longbr": "Cancelado pela plataforma",
}

TYPE_MAP: dict[str, ReturnType] = {
    "REFUND": ReturnType.REFUND_ONLY,
    "RETURN_AND_REFUND": ReturnType.RETURN_AND_REFUND,
    "REPLACEMENT": ReturnType.REPLACEMENT,
}


def _ts(value: Any) -> datetime | None:
    if value in (None, "", 0):
        return None
    return datetime.fromtimestamp(int(value), UTC)


def _money(obj: dict[str, Any] | None, key: str) -> Decimal | None:
    if not obj or obj.get(key) in (None, ""):
        return None
    try:
        return Decimal(str(obj[key]))
    except InvalidOperation:
        return None


def _deadline(r: dict[str, Any]) -> datetime | None:
    actions = r.get("seller_next_action_response") or []
    deadlines = [_ts(a.get("deadline")) for a in actions if a.get("deadline")]
    deadlines = [d for d in deadlines if d]
    return min(deadlines) if deadlines else None


def map_return(r: dict[str, Any]) -> NormalizedReturn:
    platform_status = r.get("return_status")
    refund = r.get("refund_amount") or {}

    items = []
    for li in r.get("return_line_items") or []:
        image = li.get("product_image") or {}
        items.append(
            NormalizedItem(
                external_item_id=li.get("return_line_item_id"),
                sku_id=li.get("sku_id"),
                seller_sku=li.get("seller_sku"),
                product_name=li.get("product_name"),
                sku_name=li.get("sku_name"),
                # No TikTok cada line item representa 1 unidade
                quantity=int(li.get("quantity") or 1),
                refund_amount=_money(li.get("refund_amount"), "refund_total"),
                image_url=image.get("url"),
                raw=li,
            )
        )

    return NormalizedReturn(
        external_return_id=str(r["return_id"]),
        external_order_id=r.get("order_id"),
        return_type=TYPE_MAP.get(r.get("return_type", ""), ReturnType.UNKNOWN),
        status=STATUS_MAP.get(platform_status or "", ReturnStatus.UNKNOWN),
        platform_status=platform_status,
        reason_code=r.get("return_reason"),
        reason_text=REASON_LABELS.get(r.get("return_reason") or "") or r.get("return_reason_text"),
        refund_amount=_money(refund, "refund_total"),
        currency=refund.get("currency"),
        requested_at=_ts(r.get("create_time")),
        platform_updated_at=_ts(r.get("update_time")),
        action_deadline=_deadline(r),
        tracking_number=r.get("return_tracking_number"),
        carrier=r.get("return_provider_name"),
        raw=r,
        items=items,
    )


def approve_decision(return_type: str, status: str) -> str:
    """Escolhe o `decision` do TikTok conforme o tipo e a etapa da devolução."""
    if status in (ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED):
        return "APPROVE_RECEIVED_PACKAGE"
    if return_type == ReturnType.REFUND_ONLY:
        return "APPROVE_REFUND"
    if return_type == ReturnType.REPLACEMENT:
        return "APPROVE_REPLACEMENT"
    return "APPROVE_RETURN"


def reject_decision(return_type: str, status: str) -> str:
    if status in (ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED):
        return "REJECT_RECEIVED_PACKAGE"
    if return_type == ReturnType.REFUND_ONLY:
        return "REJECT_REFUND"
    if return_type == ReturnType.REPLACEMENT:
        return "REJECT_REPLACEMENT"
    return "REJECT_RETURN"
