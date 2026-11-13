# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Confirma na Shein o recebimento das devoluções já conferidas na bancada.

Na Shein a loja não aprova nem recusa: depois que o pacote chega (status 6 Entregue / 7 Entrega pendente), a
loja confirma o recebimento (`sign-return-order`) e a devolução passa a 5 (Recebido). Se ninguém confirmar,
a Shein confirma sozinha depois de alguns dias; confirmar logo deixa a Shein igual ao que a equipe conferiu.

Roda nas rotinas. "Item diferente do pedido" não é confirmado sozinho: fica para alguém tratar na Shein.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..enums import EventType, ItemCondition, Platform
from ..models import Return, ReturnEvent, Store
from ..platforms.registry import get_adapter

log = logging.getLogger(__name__)

WAITING_RECEIPT = (6, 7)  # Entregue / Entrega pendente
LOOKBACK = timedelta(days=30)
RETRY_EVERY = timedelta(hours=6)
ACTION = "shein_sign"


def _sign_event(ok: bool, since: datetime | None = None):
    conds = [
        ReturnEvent.return_id == Return.id,
        ReturnEvent.payload["action"].astext == ACTION,
        ReturnEvent.payload["ok"].as_boolean().is_(ok),
    ]
    if since:
        conds.append(ReturnEvent.created_at > since)
    return exists().where(*conds)


def pending_receipts(db: Session, limit: int = 30) -> list[Return]:
    now = datetime.now(UTC)
    return list(db.scalars(
        select(Return)
        .where(
            Return.platform == Platform.SHEIN,
            Return.received_at > now - LOOKBACK,
            Return.item_condition != ItemCondition.MISMATCH,
            Return.raw["returnOrderStatus"].as_integer().in_(WAITING_RECEIPT),
            ~_sign_event(True),
            ~_sign_event(False, now - RETRY_EVERY),
        )
        .order_by(Return.received_at)
        .limit(limit)
    ))


def confirm_shein_receipts(db: Session, limit: int = 30) -> dict[str, int]:
    done = failed = 0
    adapters: dict = {}
    for ret in pending_receipts(db, limit):
        if ret.store_id not in adapters:
            store = db.get(Store, ret.store_id)
            adapters[ret.store_id] = (store, get_adapter(db, store))
        store, adapter = adapters[ret.store_id]
        try:
            adapter.confirm_receipt(store, ret)
        except Exception as exc:  # uma falha não impede as demais; tenta de novo em 6 h
            log.warning("Shein: recebimento da devolução %s não confirmado: %s", ret.external_return_id, exc)
            ret.events.append(ReturnEvent(
                event_type=EventType.ACTION_FAILED, source="sync",
                message=f"Shein: não consegui confirmar o recebimento: {str(exc)[:300]}",
                payload={"action": ACTION, "ok": False},
            ))
            failed += 1
        else:
            ret.events.append(ReturnEvent(
                event_type=EventType.NOTE, source="sync", message="Recebimento confirmado na Shein",
                payload={"action": ACTION, "ok": True},
            ))
            done += 1
        db.commit()
    return {"confirmadas": done, "falhas": failed}
