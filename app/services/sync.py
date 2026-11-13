# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import TERMINAL_STATUSES, EventType, Platform, ReturnStatus
from ..models import Return, ReturnEvent, ReturnItem, Store
from ..platforms.base import NormalizedReturn, PlatformNotSupported
from ..platforms.registry import get_adapter
from .buyers import buyer_key
from .costs import compute_costs
from .erp import Rule, apply_suggested_condition, load_rules

log = logging.getLogger(__name__)

# Sobreposição para não perder devoluções atualizadas durante a sync anterior
SYNC_OVERLAP = timedelta(minutes=30)


@dataclass
class SyncResult:
    store_id: str
    store_name: str
    fetched: int = 0
    created: int = 0
    updated: int = 0
    errors: list[str] = field(default_factory=list)


def effective_status(ret: Return, status: str) -> str:
    """A Shopee deixa a devolução em PROCESSING ("aguardando envio") mesmo depois que o pacote chega:
    com o rastreio dizendo entregue, ela fica como "Pacote entregue" para a loja poder responder."""
    if ret.platform == Platform.SHOPEE and status == ReturnStatus.AWAITING_BUYER_SHIPMENT and ret.logistics_status == "delivered":
        return ReturnStatus.RECEIVED
    return status


def _item_keys(items) -> list[tuple]:
    return [
        (i.external_item_id, i.sku_id, i.seller_sku, i.product_name, i.sku_name, i.quantity, i.refund_amount, i.image_url, i.raw)
        for i in items
    ]


def upsert_return(
    db: Session, store: Store, nr: NormalizedReturn, source: str, rules: list[Rule] | None = None
) -> tuple[Return, bool]:
    """Cria ou atualiza a devolução local. Retorna (devolução, criada?).

    Campos de decisão local (motivo interno, notas, quem decidiu, condição conferida) nunca são sobrescritos.
    """
    ret = db.scalar(
        select(Return).where(Return.store_id == store.id, Return.external_return_id == nr.external_return_id)
    )
    if ret is None and nr.external_order_id:
        # Importadas do sistema antigo sem return_sn ficaram como "pedido-<nº>": adota o registro
        ret = db.scalar(
            select(Return).where(
                Return.store_id == store.id, Return.external_return_id == f"pedido-{nr.external_order_id}"
            )
        )
        if ret is not None:
            ret.external_return_id = nr.external_return_id
    created = ret is None
    if created:
        ret = Return(store_id=store.id, platform=store.platform, external_return_id=nr.external_return_id)
        db.add(ret)
        previous_status = None
    else:
        # Ignora dados mais antigos que os já gravados (ex.: webhook chegando fora de ordem)
        # (exceto importadas do sistema antigo, cujo updated_at era da própria ferramenta, não da plataforma)
        legacy_only = "legacy" in (ret.raw or {}) and len(ret.raw) == 1
        if (
            not legacy_only
            and ret.platform_updated_at
            and nr.platform_updated_at
            and nr.platform_updated_at < ret.platform_updated_at
        ):
            return ret, False
        previous_status = ret.status

    ret.external_order_id = nr.external_order_id
    ret.return_type = nr.return_type
    status = effective_status(ret, nr.status)
    ret.status = status
    ret.platform_status = nr.platform_status
    ret.reason_code = nr.reason_code
    ret.reason_text = nr.reason_text
    ret.refund_amount = nr.refund_amount
    ret.currency = nr.currency
    ret.requested_at = nr.requested_at
    ret.platform_updated_at = nr.platform_updated_at
    ret.action_deadline = nr.action_deadline
    ret.tracking_number = nr.tracking_number
    ret.carrier = nr.carrier
    # Mantém os dados do sistema antigo (fotos, observações) nas devoluções importadas
    legacy = (ret.raw or {}).get("legacy")
    ret.raw = {**nr.raw, "legacy": legacy} if legacy else nr.raw
    ret.buyer_key = buyer_key(store.platform, nr.raw) or ret.buyer_key
    apply_suggested_condition(ret, rules if rules is not None else load_rules(db))

    if status in TERMINAL_STATUSES and ret.resolved_at is None:
        ret.resolved_at = nr.platform_updated_at or datetime.now(UTC)
    elif status not in TERMINAL_STATUSES:
        ret.resolved_at = None

    # O TikTok reenvia o mesmo aviso várias vezes: itens iguais não são apagados e recriados
    if _item_keys(ret.items) != _item_keys(nr.items):
        ret.items = [
            ReturnItem(
                external_item_id=i.external_item_id,
                sku_id=i.sku_id,
                seller_sku=i.seller_sku,
                product_name=i.product_name,
                sku_name=i.sku_name,
                quantity=i.quantity,
                refund_amount=i.refund_amount,
                image_url=i.image_url,
                raw=i.raw,
            )
            for i in nr.items
        ]
    compute_costs(ret)

    if created:
        ret.events.append(ReturnEvent(event_type=EventType.CREATED, source=source, to_status=status))
    elif previous_status != status:
        ret.events.append(
            ReturnEvent(
                event_type=EventType.STATUS_CHANGED,
                source=source,
                from_status=previous_status,
                to_status=status,
                message=nr.platform_status,
            )
        )
    return ret, created


def sync_store(db: Session, store: Store, since: datetime | None = None) -> SyncResult:
    result = SyncResult(store_id=str(store.id), store_name=store.name)
    now = datetime.now(UTC)
    if since is None:
        since = (
            store.last_synced_at - SYNC_OVERLAP
            if store.last_synced_at
            else now - timedelta(days=get_settings().sync_lookback_days)
        )

    try:
        adapter = get_adapter(db, store)
        rules = load_rules(db)
        for nr in adapter.fetch_returns(store, since, now):
            result.fetched += 1
            _, created = upsert_return(db, store, nr, source="sync", rules=rules)
            if created:
                result.created += 1
            else:
                result.updated += 1
            # Grava aos poucos: se a plataforma falhar no meio, o que já veio não se perde
            if result.fetched % 20 == 0:
                db.commit()
        store.last_synced_at = now
        store.last_sync_error = store.sync_error_since = None
        db.commit()
    except PlatformNotSupported:
        db.rollback()  # loja autorizada, mas a busca de devoluções dessa plataforma ainda não existe
    except Exception as exc:
        db.rollback()
        log.exception("Falha ao sincronizar loja %s", store.name)
        result.errors.append(str(exc))
        # Guarda o motivo para a fila, Integrações e o e-mail mostrarem por que a loja parou
        store.last_sync_error = str(exc)[:1000]
        store.sync_error_since = store.sync_error_since or now
        db.commit()
    return result


def sync_all(db: Session) -> list[SyncResult]:
    stores = db.scalars(select(Store).where(Store.active)).all()
    return [sync_store(db, s) for s in stores]
