# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Revisão das devoluções do Mercado Livre ("Já revisei") depois da conferência na bancada.

O ML reembolsa o comprador e depois dá alguns dias para a loja revisar a peça que voltou. Se a loja não
responder no prazo, o ML entende que chegou como esperado; responder logo deixa o ML igual ao que a equipe
conferiu. Roda nas rotinas:

- conferida como revenda -> revisão OK (`return_review_ok`), sozinha;
- defeito ou item diferente -> não responde sozinha: a revisão com problema leva fotos e motivo, e é feita
  pela tela da devolução ("Contestar"), dentro do prazo.
"""

import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import exists, select
from sqlalchemy.orm import Session

from ..enums import EventType, ItemCondition, Platform, ReturnStatus
from ..models import Return, ReturnEvent, Store
from ..platforms.registry import get_adapter

log = logging.getLogger(__name__)

LOOKBACK = timedelta(days=30)
RETRY_EVERY = timedelta(hours=6)
ACTION = "ml_review_ok"


def _review_event(ok: bool, since: datetime | None = None):
    conds = [
        ReturnEvent.return_id == Return.id,
        ReturnEvent.payload["action"].astext == ACTION,
        ReturnEvent.payload["ok"].as_boolean().is_(ok),
    ]
    if since:
        conds.append(ReturnEvent.created_at > since)
    return exists().where(*conds)


def pending_reviews(db: Session, limit: int = 30) -> list[Return]:
    now = datetime.now(UTC)
    return list(db.scalars(
        select(Return)
        .where(
            Return.platform == Platform.MERCADO_LIVRE,
            Return.received_at > now - LOOKBACK,
            Return.item_condition == ItemCondition.RESALE,
            # Revisão aberta no ML (a reclamação pode estar fechada, mas a revisão não)
            Return.status.in_((ReturnStatus.RECEIVED, ReturnStatus.IN_TRANSIT)),
            ~_review_event(True),
            ~_review_event(False, now - RETRY_EVERY),
        )
        .order_by(Return.received_at)
        .limit(limit)
    ))


def review_ml_returns(db: Session, limit: int = 30) -> dict[str, int]:
    done = failed = 0
    adapters: dict = {}
    for ret in pending_reviews(db, limit):
        if ret.store_id not in adapters:
            store = db.get(Store, ret.store_id)
            adapters[ret.store_id] = (store, get_adapter(db, store))
        store, adapter = adapters[ret.store_id]
        try:
            adapter.approve(store, ret, decision="return_review_ok")
        except Exception as exc:  # uma falha não impede as demais; tenta de novo em 6 h
            log.warning("ML: revisão da devolução %s não enviada: %s", ret.external_return_id, exc)
            ret.events.append(ReturnEvent(
                event_type=EventType.ACTION_FAILED, source="sync",
                message=f"Mercado Livre: não consegui marcar a revisão como OK: {str(exc)[:300]}",
                payload={"action": ACTION, "ok": False},
            ))
            failed += 1
        else:
            ret.events.append(ReturnEvent(
                event_type=EventType.APPROVED, source="sync",
                message="Revisão enviada ao Mercado Livre: chegou como esperado (conferida como revenda)",
                payload={"action": ACTION, "ok": True},
            ))
            done += 1
        db.commit()
    return {"revisadas": done, "falhas": failed}
