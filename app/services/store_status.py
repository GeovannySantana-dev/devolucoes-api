# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Situação de cada loja conectada: sincronizando, autorização vencendo ou parada (e por quê).

Usada no aviso da fila (só gestão), no selo de cada loja em Integrações e no e-mail de loja parada.
"""

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Store

STALE_AFTER = timedelta(hours=2)  # rotinas a cada 15 min: 2 h sem sincronizar já é problema
EXPIRING = timedelta(days=7)


@dataclass
class StoreStatus:
    store: Store
    level: str  # ok, warn, bad, off
    label: str
    detail: str


def since_text(delta: timedelta) -> str:
    hours = int(delta.total_seconds() // 3600)
    if hours < 1:
        return f"{max(int(delta.total_seconds() // 60), 1)} min"
    if hours < 48:
        return f"{hours} h"
    return f"{hours // 24} dias"


def reason(store: Store) -> str:
    """O motivo da falha sem o código técnico da plataforma ("... [openapi00001: ...] (/open-api/...)")."""
    text = (store.last_sync_error or "").split(" [", 1)[0].strip()
    for prefix in ("Shein: ", "Shopee: ", "TikTok: ", "Mercado Livre: "):
        text = text.removeprefix(prefix)
    return text[:160]


def status(store: Store, now: datetime | None = None) -> StoreStatus:
    now = now or datetime.now(UTC)
    if not store.active:
        return StoreStatus(store, "off", "pausada", "não sincroniza até ser autorizada de novo")
    last = store.last_synced_at
    if last is None or now - last > STALE_AFTER:
        since = store.sync_error_since or last
        label = f"parada há {since_text(now - since)}" if since else "nunca sincronizou"
        why = reason(store) or "sem resposta da plataforma"
        return StoreStatus(store, "bad", label, why)
    expires = store.refresh_token_expires_at
    if expires and expires - now < EXPIRING:
        days = max((expires - now).days, 0)
        return StoreStatus(store, "warn", f"vence em {days} dia{'s' if days != 1 else ''}" if days else "vence hoje",
                           f"autorização vale até {expires:%d/%m}; renove para não parar")
    return StoreStatus(store, "ok", "sincronizando", "")


def all_statuses(db: Session, now: datetime | None = None) -> list[StoreStatus]:
    stores = db.scalars(select(Store).order_by(Store.platform, Store.name)).all()
    return [status(s, now) for s in stores]


def stopped(db: Session, now: datetime | None = None) -> list[StoreStatus]:
    """Lojas ativas paradas: o aviso de uma linha no topo da fila."""
    return [st for st in all_statuses(db, now) if st.level == "bad"]
