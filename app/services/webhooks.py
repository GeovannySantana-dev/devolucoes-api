import json
import logging
import re
import uuid
from datetime import UTC, datetime
from typing import Any

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from ..config import get_settings
from ..db import new_session
from ..enums import Platform
from ..models import Store, WebhookEvent
from ..platforms.registry import get_adapter
from .sync import upsert_return

log = logging.getLogger(__name__)

# Tipos de webhook do TikTok Shop relevantes para devoluções
TIKTOK_RETURN_STATUS_CHANGE = 12
# Tópicos do Mercado Livre sobre reclamações/devoluções
MERCADOLIVRE_CLAIM_TOPICS = {"claims", "claims_actions"}


def store_tiktok_event(raw_body: bytes) -> uuid.UUID | None:
    """Grava o webhook e devolve o id para processamento. None = duplicado ou irrelevante."""
    payload: dict[str, Any] = json.loads(raw_body)
    data = payload.get("data") or {}
    with new_session() as db:
        store = db.scalar(
            select(Store).where(Store.platform == Platform.TIKTOK, Store.external_shop_id == str(payload.get("shop_id")))
        )
        evt = WebhookEvent(
            platform=Platform.TIKTOK,
            store_id=store.id if store else None,
            notification_id=payload.get("tts_notification_id"),
            event_type=str(payload.get("type")),
            external_id=data.get("return_id"),
            payload=payload,
        )
        db.add(evt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            log.info("Webhook duplicado ignorado: %s", payload.get("tts_notification_id"))
            return None

        if payload.get("type") != TIKTOK_RETURN_STATUS_CHANGE or store is None:
            evt.processed_at = datetime.now(UTC)
            evt.error = None if store else f"Loja shop_id={payload.get('shop_id')} não cadastrada"
            db.commit()
            return None
        return evt.id


def mercadolivre_claim_id(resource: str | None) -> str | None:
    """'/post-purchase/v1/claims/123' (ou '/v1/claims/123/...') -> '123'."""
    match = re.search(r"/claims/(\d+)", resource or "")
    return match.group(1) if match else None


def store_mercadolivre_event(payload: dict[str, Any]) -> uuid.UUID | None:
    """Grava a notificação do Mercado Livre e devolve o id para processamento.

    Todas as notificações ficam em webhook_events (servem a outros usos no futuro); só as de
    reclamações são processadas. None = duplicada, de outro app ou irrelevante.
    """
    s = get_settings()
    if str(payload.get("application_id")) != s.mercadolivre_client_id:
        log.warning("Notificação do Mercado Livre de outro app ignorada: %s", payload.get("application_id"))
        return None

    claim_id = mercadolivre_claim_id(payload.get("resource"))
    with new_session() as db:
        store = db.scalar(
            select(Store).where(
                Store.platform == Platform.MERCADO_LIVRE, Store.external_shop_id == str(payload.get("user_id"))
            )
        )
        evt = WebhookEvent(
            platform=Platform.MERCADO_LIVRE,
            store_id=store.id if store else None,
            # O mesmo aviso pode ser reenviado (attempts > 1) com o mesmo _id
            notification_id=payload.get("_id") or f"{payload.get('resource')}@{payload.get('sent')}",
            event_type=payload.get("topic"),
            external_id=claim_id,
            payload=payload,
        )
        db.add(evt)
        try:
            db.commit()
        except IntegrityError:
            db.rollback()
            return None

        if store is None or not claim_id or payload.get("topic") not in MERCADOLIVRE_CLAIM_TOPICS:
            evt.processed_at = datetime.now(UTC)
            evt.error = None if store else f"Conta user_id={payload.get('user_id')} não cadastrada"
            db.commit()
            return None
        return evt.id


def process_tiktok_event(event_id: uuid.UUID) -> None:
    """Busca a devolução atualizada na API do TikTok e grava localmente."""
    process_event(event_id)


def process_pending_events(limit: int = 200) -> int:
    """Processa notificações gravadas e ainda não processadas (rede de segurança do segundo plano)."""
    with new_session() as db:
        ids = db.scalars(
            select(WebhookEvent.id)
            .where(WebhookEvent.processed_at.is_(None), WebhookEvent.store_id.is_not(None))
            .order_by(WebhookEvent.received_at)
            .limit(limit)
        ).all()
    for event_id in ids:
        process_event(event_id)
    return len(ids)


def process_event(event_id: uuid.UUID) -> None:
    """Busca a devolução atualizada na plataforma do evento e grava localmente."""
    with new_session() as db:
        evt = db.get(WebhookEvent, event_id)
        if evt is None or evt.processed_at:
            return
        try:
            store = db.get(Store, evt.store_id)
            adapter = get_adapter(db, store)
            nr = adapter.fetch_return(store, evt.external_id)
            if nr:
                try:
                    upsert_return(db, store, nr, source="webhook")
                    db.flush()
                except IntegrityError:
                    # Outro aviso da mesma devolução (ou a sincronização) a criou ao mesmo tempo:
                    # agora ela já existe, então basta atualizar.
                    db.rollback()
                    store = db.get(Store, evt.store_id)
                    upsert_return(db, store, nr, source="webhook")
            evt.error = None if nr else "Devolução não encontrada na API"
        except Exception as exc:
            db.rollback()
            log.exception("Falha ao processar webhook %s", event_id)
            evt = db.get(WebhookEvent, event_id)
            evt.error = str(exc)
        evt.processed_at = datetime.now(UTC)
        db.commit()
