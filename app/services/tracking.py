# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Rastreio do pacote de volta (logística reversa).

Shopee: a lista de devoluções não traz a logística e a Devolução Fácil aceita antes de o pacote chegar,
então consultamos `returns/get_reverse_tracking_info` das devoluções físicas ainda não conferidas.
O resultado fica em colunas próprias (logistics_*), que a sincronização não sobrescreve.
"""

import logging
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..enums import EventType, Platform, ReturnStatus, ReturnType
from ..models import Return, ReturnEvent, Store
from ..platforms.base import PlatformError
from ..platforms.shopee.client import ShopeeAPIError
from .sync import effective_status

log = logging.getLogger(__name__)

# Situações finais: não precisa consultar de novo
FINAL = {"delivered", "cancelled", "lost"}
RECHECK_AFTER = timedelta(hours=3)
LOOKBACK = timedelta(days=45)
BATCH_SIZE = 150

SHOPEE_LOGISTICS = {
    "LOGISTICS_REQUEST_CREATED": "awaiting_shipment",
    "LOGISTICS_PENDING_ARRANGE": "awaiting_shipment",
    "LOGISTICS_READY": "awaiting_shipment",
    "LOGISTICS_PICKUP_RETRY": "awaiting_shipment",
    "LOGISTICS_PICKUP_DONE": "in_transit",
    "LOGISTICS_PICKUP_FAILED": "awaiting_shipment",
    "LOGISTICS_DELIVERY_DONE": "delivered",
    "LOGISTICS_DELIVERY_FAILED": "failed",
    "LOGISTICS_LOST": "lost",
    "LOGISTICS_REQUEST_CANCELED": "cancelled",
}
# Sem situação conhecida: classifica pelo texto da última movimentação (como o sistema antigo)
_TEXT_RULES = (
    ("aguardando postagem", "awaiting_shipment"),
    ("pedido devolvido", "delivered"),
    ("entregue ao remetente", "delivered"),
    ("saiu para entrega", "in_transit"),
    ("em rota de entrega", "in_transit"),
    ("postagem", "in_transit"),
    ("centro logístico", "in_transit"),
    ("centro de", "in_transit"),
)


def _ts(value: Any) -> datetime | None:
    return datetime.fromtimestamp(int(value), UTC) if value else None


def map_shopee_tracking(resp: dict[str, Any]) -> dict[str, Any]:
    """Resposta de get_reverse_tracking_info -> campos logistics_* normalizados."""
    events = sorted(resp.get("tracking_info") or [], key=lambda e: e.get("update_time") or 0, reverse=True)
    last = events[0] if events else {}
    detail = " ".join((last.get("tracking_description") or "").split()) or None
    status = SHOPEE_LOGISTICS.get(resp.get("reverse_logistics_status") or "")
    if status is None and detail:
        text = detail.lower()
        status = next((s for key, s in _TEXT_RULES if key in text), None)
    if status is None and events:
        status = "in_transit"  # há movimentação, mas sem situação reconhecida
    return {
        "logistics_status": status,
        "logistics_detail": detail,
        "logistics_event_at": _ts(last.get("update_time") or resp.get("reverse_logistics_update_time")),
        "tracking_number": resp.get("tracking_number") or None,
    }


def apply_tracking(ret: Return, data: dict[str, Any], now: datetime) -> bool:
    """Grava o rastreio. Retorna True se a situação do pacote mudou."""
    changed = data["logistics_status"] != ret.logistics_status
    previous = ret.logistics_status
    ret.logistics_status = data["logistics_status"]
    ret.logistics_detail = data["logistics_detail"]
    ret.logistics_event_at = data["logistics_event_at"]
    ret.logistics_checked_at = now
    if data.get("tracking_number") and not ret.tracking_number:
        ret.tracking_number = data["tracking_number"]
    # Pacote entregue: a devolução da Shopee em PROCESSING passa a "Pacote entregue" (ver sync.effective_status)
    status = effective_status(ret, ret.status)
    if status != ret.status:
        ret.events.append(ReturnEvent(event_type=EventType.STATUS_CHANGED, source="sync", from_status=ret.status,
                                      to_status=status, message="Pacote entregue (rastreio)"))
        ret.status = status
    if changed and data["logistics_status"] in ("in_transit", "delivered", "failed", "lost"):
        ret.events.append(
            ReturnEvent(
                event_type=EventType.TRACKING,
                source="sync",
                message=data["logistics_detail"],
                payload={"from": previous, "to": data["logistics_status"]},
            )
        )
    return changed


def _pending(db: Session, now: datetime, batch: int) -> list[Return]:
    return db.scalars(
        select(Return)
        .where(
            Return.platform == Platform.SHOPEE,
            Return.return_type != ReturnType.REFUND_ONLY,
            Return.received_at.is_(None),
            Return.handled_outside_at.is_(None),
            Return.status.not_in((ReturnStatus.CANCELLED, ReturnStatus.REJECTED)),
            Return.requested_at >= now - LOOKBACK,
            or_(Return.logistics_status.is_(None), Return.logistics_status.not_in(FINAL)),
            or_(Return.logistics_checked_at.is_(None), Return.logistics_checked_at < now - RECHECK_AFTER),
        )
        .order_by(Return.logistics_checked_at.asc().nulls_first(), Return.requested_at.desc())
        .limit(batch)
    ).all()


def refresh_shopee_tracking(db: Session, batch: int = BATCH_SIZE) -> dict[str, int]:
    """Job: atualiza o rastreio das devoluções da Shopee a caminho (até `batch` por rodada)."""
    from ..platforms.registry import get_shopee_client
    from .tokens import ensure_shopee_token

    now = datetime.now(UTC)
    client = get_shopee_client()
    result = {"checked": 0, "changed": 0, "delivered": 0, "errors": 0}
    tokens: dict[Any, str] = {}

    for i, ret in enumerate(_pending(db, now, batch), start=1):
        try:
            if ret.store_id not in tokens:
                tokens[ret.store_id] = ensure_shopee_token(db, db.get(Store, ret.store_id))
            store = db.get(Store, ret.store_id)
            resp = client.get_reverse_tracking(tokens[ret.store_id], store.external_shop_id, ret.external_return_id)
            data = map_shopee_tracking(resp)
        except ShopeeAPIError as exc:
            if "reverse_logistics" not in str(exc.error):
                log.warning("Rastreio Shopee %s: %s", ret.external_return_id, exc)
                result["errors"] += 1
            # Ainda sem logística reversa (comprador não postou): tenta de novo mais tarde
            ret.logistics_status = ret.logistics_status or "none"
            ret.logistics_checked_at = now
            continue
        except PlatformError as exc:
            log.warning("Rastreio Shopee %s: %s", ret.external_return_id, exc)
            result["errors"] += 1
            continue
        result["checked"] += 1
        if apply_tracking(ret, data, now):
            result["changed"] += 1
        if data["logistics_status"] == "delivered":
            result["delivered"] += 1
        if i % 20 == 0:
            db.commit()
    db.commit()
    return result
