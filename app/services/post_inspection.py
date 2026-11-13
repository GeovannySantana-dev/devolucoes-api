"""Leva para o marketplace o resultado da conferência das devoluções boas para revenda.

Depois que o pacote chega, cada plataforma espera uma resposta da loja até um prazo (sem resposta, ela decide
sozinha a favor do comprador). Conferida como revenda, a resposta é sempre "chegou certo", então o app responde
sozinho nas rotinas:

- TikTok: aprova o pacote recebido (APPROVE_RECEIVED_PACKAGE), que libera o reembolso;
- Shopee: confirma a devolução (returns/confirm). A Shopee deixa a devolução em PROCESSING mesmo com o pacote
  entregue, por isso vale também para "aguardando envio" quando o rastreio diz entregue;
- Mercado Livre: revisão OK, em ml_review;
- Shein: confirmação de recebimento, em shein_receipt.

Defeito e item diferente não são respondidos sozinhos: contestar leva fotos e motivo e é feito pela tela.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, or_, select
from sqlalchemy.orm import Session, selectinload

from ..enums import Decision, EventType, ItemCondition, Platform, ReturnStatus
from ..models import Return, ReturnEvent, Store
from ..platforms.registry import get_adapter
from .actions import _refresh_from_platform

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=30)
RETRY_EVERY = timedelta(hours=6)
ACTION = "post_inspection_ok"
ACTOR = "Sistema (conferência)"
WAITING = {
    Platform.TIKTOK: (ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED),
    Platform.SHOPEE: (ReturnStatus.AWAITING_BUYER_SHIPMENT, ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED,
                      ReturnStatus.PENDING_REVIEW),
}


def _event(ok: bool, since: datetime | None = None):
    conds = [
        ReturnEvent.return_id == Return.id,
        ReturnEvent.payload["action"].astext == ACTION,
        ReturnEvent.payload["ok"].as_boolean().is_(ok),
    ]
    if since:
        conds.append(ReturnEvent.created_at > since)
    return exists().where(*conds)


def pending(db: Session, limit: int = 40) -> list[Return]:
    now = datetime.now(UTC)
    per_platform = [(Return.platform == p) & Return.status.in_(statuses) for p, statuses in WAITING.items()]
    return list(db.scalars(
        select(Return)
        .where(
            or_(*per_platform),
            Return.received_at > now - LOOKBACK,
            Return.item_condition == ItemCondition.RESALE,
            Return.decision.is_(None),
            ~_event(True),
            ~_event(False, now - RETRY_EVERY),
        )
        .options(selectinload(Return.store))
        .order_by(Return.action_deadline.asc().nulls_last())
        .limit(limit)
    ))


def confirm_inspected(db: Session, limit: int = 40) -> dict[str, int]:
    done = failed = 0
    adapters: dict = {}
    for ret in pending(db, limit):
        if ret.store_id not in adapters:
            adapters[ret.store_id] = get_adapter(db, db.get(Store, ret.store_id))
        try:
            adapters[ret.store_id].approve(ret.store, ret, None)
        except Exception as exc:  # uma falha não impede as demais; tenta de novo em 6 h
            log.warning("Aprovação depois da conferência falhou (%s %s): %s", ret.platform, ret.external_return_id, exc)
            ret.events.append(ReturnEvent(
                event_type=EventType.ACTION_FAILED, source="sync", actor=ACTOR,
                message=f"Não consegui aprovar no marketplace depois da conferência: {str(exc)[:300]}",
                payload={"action": ACTION, "ok": False},
            ))
            failed += 1
            db.commit()
            continue
        now = datetime.now(UTC)
        ret.decision, ret.decided_at, ret.decided_by = Decision.APPROVED, now, ACTOR
        ret.events.append(ReturnEvent(
            event_type=EventType.APPROVED, source="sync", actor=ACTOR, from_status=ret.status,
            message="Aprovada no marketplace: conferida como revenda (chegou certo)",
            payload={"action": ACTION, "ok": True},
        ))
        db.commit()
        _refresh_from_platform(db, ret)
        db.commit()
        done += 1
    return {"aprovadas": done, "falhas": failed}
