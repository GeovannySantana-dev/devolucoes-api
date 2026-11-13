# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import logging
from datetime import UTC, datetime, timedelta

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Platform
from ..models import Store
from ..platforms.base import PlatformError, TokenSet

log = logging.getLogger(__name__)

REFRESH_MARGIN = timedelta(hours=24)
# O access_token da Shopee dura só 4 horas
SHOPEE_REFRESH_MARGIN = timedelta(minutes=30)


def apply_token_set(store: Store, ts: TokenSet) -> None:
    store.access_token = ts.access_token
    store.access_token_expires_at = ts.access_token_expires_at
    store.refresh_token = ts.refresh_token
    store.refresh_token_expires_at = ts.refresh_token_expires_at


def _needs_refresh(store: Store, margin: timedelta) -> bool:
    expires = store.access_token_expires_at
    return expires is None or expires - datetime.now(UTC) < margin


def ensure_tiktok_token(db: Session, store: Store) -> str:
    """Retorna um access token válido, renovando se estiver perto de expirar."""
    from ..platforms.registry import get_tiktok_client

    if not store.access_token or not store.refresh_token:
        raise PlatformError(f"Loja {store.name} não está autorizada no TikTok Shop")

    if _needs_refresh(store, REFRESH_MARGIN):
        log.info("Renovando token TikTok da loja %s", store.name)
        apply_token_set(store, get_tiktok_client(store.tiktok_app or 1).refresh_token(store.refresh_token))
        db.commit()
    return store.access_token


def ensure_shopee_token(db: Session, store: Store) -> str:
    from ..platforms.registry import get_shopee_client

    if not store.access_token or not store.refresh_token:
        raise PlatformError(f"Loja {store.name} não está autorizada na Shopee")

    if _needs_refresh(store, SHOPEE_REFRESH_MARGIN):
        log.info("Renovando token Shopee da loja %s", store.name)
        # A Shopee devolve um refresh_token novo a cada renovação; o antigo deixa de valer
        apply_token_set(store, get_shopee_client().refresh_token(store.refresh_token, store.external_shop_id))
        db.commit()
    return store.access_token


def ensure_mercadolivre_token(db: Session, store: Store) -> str:
    from ..platforms.registry import get_mercadolivre_client

    if not store.access_token or not store.refresh_token:
        raise PlatformError(f"Loja {store.name} não está autorizada no Mercado Livre")

    # O access_token dura 6 horas; o refresh_token é trocado a cada renovação
    if _needs_refresh(store, SHOPEE_REFRESH_MARGIN):
        log.info("Renovando token Mercado Livre da loja %s", store.name)
        apply_token_set(store, get_mercadolivre_client().refresh_token(store.refresh_token))
        db.commit()
    return store.access_token


def refresh_expiring_tokens(db: Session) -> None:
    ensure = {
        Platform.TIKTOK: ensure_tiktok_token,
        Platform.SHOPEE: ensure_shopee_token,
        Platform.MERCADO_LIVRE: ensure_mercadolivre_token,
    }
    stores = db.scalars(
        select(Store).where(Store.platform.in_(list(ensure)), Store.active, Store.refresh_token.is_not(None))
    ).all()
    for store in stores:
        try:
            ensure[store.platform](db, store)
        except Exception:
            log.exception("Falha ao renovar token da loja %s", store.name)
