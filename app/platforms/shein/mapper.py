# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Converte as devoluções da Shein (return-order/details) para o modelo normalizado.

Na Shein BR as devoluções já entram aceitas: o comprador manda o pacote para o armazém da Shein, que repassa
à loja. A loja não aceita nem recusa; só confere e confirma o recebimento. Horários vêm em GMT+8.
"""

import re
from datetime import datetime, timedelta, timezone
from decimal import Decimal
from typing import Any

from ...enums import ReturnStatus, ReturnType
from ..base import NormalizedItem, NormalizedReturn

BEIJING = timezone(timedelta(hours=8))

# returnOrderStatus -> status unificado. "A caminho" (IN_TRANSIT) faz a devolução aparecer em "Para conferir".
STATUS = {
    2: ReturnStatus.AWAITING_BUYER_SHIPMENT,  # Já aplicado: aceita, comprador ainda vai enviar
    8: ReturnStatus.IN_TRANSIT,  # Aguardando trânsito no armazém Shein
    7: ReturnStatus.IN_TRANSIT,  # Entrega pendente: armazém da Shein enviando para a loja
    6: ReturnStatus.IN_TRANSIT,  # Entregue pela transportadora; falta a loja conferir e confirmar
    5: ReturnStatus.COMPLETED,  # Recebido: loja confirmou o recebimento
    9: ReturnStatus.COMPLETED,  # Concluído (reembolso sem devolução / item faltante)
    1: ReturnStatus.CANCELLED,  # Fechado: cancelada automaticamente
    3: ReturnStatus.CANCELLED,  # Cancelada pelo cliente
}
STATUS_LABEL = {2: "Já aplicado", 8: "Aguardando trânsito no armazém Shein", 7: "Entrega pendente",
                6: "Entregue", 5: "Recebido", 9: "Concluído", 1: "Fechado", 3: "Cancelado"}


def parse_time(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BEIJING)
    except ValueError:
        return None


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def _pt(entries: list[dict[str, Any]] | None, field: str) -> str | None:
    by_lang = {e.get("language"): e.get(field) for e in entries or []}
    text = by_lang.get("PT") or by_lang.get("EN")
    return " ".join(str(text).split()) if text and text != "-" else None


def main_reason(text: str | None) -> str | None:
    """A Shein junta motivo e detalhe sem espaço ("Tamanho muito grandeO ajuste..."): fica só o motivo."""
    if not text:
        return None
    m = re.search(r"[a-zà-ú)][A-ZÀ-Ú]", text)
    return text[: m.start() + 1].strip() if m else text


def item_refund(goods: dict[str, Any]) -> Decimal:
    """Valor pago pela peça: preço menos promoção e cupom da loja."""
    return (
        _d(goods.get("sellerCurrencyPrice"))
        - _d(goods.get("sellerCurrencyPromotionPrice"))
        - _d(goods.get("sellerCurrencyStoreCouponPrice"))
    )


def map_return(d: dict[str, Any]) -> NormalizedReturn:
    goods = d.get("returnGoodsInfoList") or []
    code = d.get("returnOrderStatus")
    items = [
        NormalizedItem(
            external_item_id=str(g.get("goodsId") or "") or None,
            sku_id=g.get("sku"),
            seller_sku=g.get("skuSn") or g.get("goodsSn"),
            product_name=g.get("goodsTitle"),
            sku_name=_pt(g.get("commodityAttributeList"), "attrName"),
            quantity=1,  # a Shein lista uma linha por peça
            refund_amount=item_refund(g),
            image_url=g.get("imageUrl"),
            raw=g,
        )
        for g in goods
    ]
    first = goods[0] if goods else {}
    reason_pt = _pt(first.get("returnReasonList"), "reason")
    reason_en = next((r.get("reason") for r in first.get("returnReasonList") or [] if r.get("language") == "EN"), None)
    return NormalizedReturn(
        external_return_id=d["returnOrderNo"],
        external_order_id=d.get("orderNo"),
        return_type=ReturnType.REFUND_ONLY if d.get("noReturnGoodsSign") == 1 else ReturnType.RETURN_AND_REFUND,
        status=STATUS.get(code, ReturnStatus.UNKNOWN),
        platform_status=STATUS_LABEL.get(code, str(code)),
        reason_code=reason_en,
        reason_text=main_reason(reason_pt),
        refund_amount=sum((i.refund_amount or Decimal(0) for i in items), Decimal(0)),
        currency=first.get("currency") or "BRL",
        requested_at=parse_time(d.get("requestReturnTime")),
        platform_updated_at=parse_time(d.get("lastUpdateTime")),
        action_deadline=None,  # a loja não decide na Shein
        tracking_number=d.get("platformExpressNo") or d.get("memberExpressNo") or None,
        carrier=d.get("expressCompanyName") or None,
        raw=d,
        items=items,
    )
