# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from functools import lru_cache

from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import Platform
from ..models import Store
from .base import PlatformAdapter, PlatformError, PlatformNotSupported
from .mercadolivre.adapter import MercadoLivreAdapter
from .mercadolivre.client import MercadoLivreClient
from .shopee.adapter import ShopeeAdapter
from .shopee.client import ShopeeClient
from .tiktok.adapter import TikTokAdapter
from .tiktok.client import TikTokClient


@lru_cache
def get_tiktok_client(app: int = 1) -> TikTokClient:
    s = get_settings()
    if app == 2:
        if not (s.tiktok2_app_key and s.tiktok2_app_secret):
            raise PlatformError("Segundo app do TikTok não configurado (TIKTOK2_APP_KEY e TIKTOK2_APP_SECRET)")
        return TikTokClient(s.tiktok2_app_key, s.tiktok2_app_secret, s.tiktok_api_base, s.tiktok_auth_base)
    return TikTokClient(s.tiktok_app_key, s.tiktok_app_secret, s.tiktok_api_base, s.tiktok_auth_base)


@lru_cache
def get_mercadolivre_client() -> MercadoLivreClient:
    s = get_settings()
    return MercadoLivreClient(
        s.mercadolivre_client_id, s.mercadolivre_client_secret, s.mercadolivre_api_base, s.mercadolivre_auth_url
    )


@lru_cache
def get_shopee_client() -> ShopeeClient:
    s = get_settings()
    return ShopeeClient(s.shopee_partner_id, s.shopee_partner_key, s.shopee_api_base)


def get_shein_client() -> "SheinClient":
    from .shein.client import SheinClient

    s = get_settings()
    return SheinClient(s.shein_app_id, s.shein_app_secret, s.shein_api_base, s.shein_auth_url)


def get_adapter(db: Session, store: Store) -> PlatformAdapter:
    # import local para evitar ciclo services <-> platforms
    from ..services.tokens import ensure_mercadolivre_token, ensure_shopee_token, ensure_tiktok_token

    if store.platform == Platform.TIKTOK:
        from ..services.media import evidence_photos

        return TikTokAdapter(
            get_tiktok_client(store.tiktok_app or 1),
            token_provider=lambda s: ensure_tiktok_token(db, s),
            locale=get_settings().tiktok_locale,
            evidence_provider=evidence_photos,
        )
    if store.platform == Platform.SHOPEE:
        from ..services.media import evidence_photos

        return ShopeeAdapter(
            get_shopee_client(),
            token_provider=lambda s: ensure_shopee_token(db, s),
            dispute_email=get_settings().shopee_dispute_email,
            evidence_provider=evidence_photos,
        )
    if store.platform == Platform.MERCADO_LIVRE:
        from ..services.media import evidence_photos

        return MercadoLivreAdapter(
            get_mercadolivre_client(),
            token_provider=lambda s: ensure_mercadolivre_token(db, s),
            evidence_provider=evidence_photos,
        )
    if store.platform == Platform.SHEIN:
        from .shein.adapter import SheinAdapter

        return SheinAdapter(get_shein_client())
    raise PlatformNotSupported(f"Integração com {store.platform} ainda não implementada")
