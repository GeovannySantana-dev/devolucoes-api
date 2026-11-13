from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Platform
from ..models import Store
from ..platforms.registry import get_shopee_client
from .tokens import apply_token_set


def authorize_with_code(db: Session, code: str, shop_id: str) -> Store:
    """Troca o código de autorização por tokens e cadastra/atualiza a loja.

    A loja importada do sistema antigo (mesmo shop_id) é reaproveitada, mantendo o histórico.
    """
    client = get_shopee_client()
    tokens = client.exchange_code(code, shop_id)
    info = client.get_shop_info(tokens.access_token, shop_id)

    store = db.scalar(select(Store).where(Store.platform == Platform.SHOPEE, Store.external_shop_id == str(shop_id)))
    if store is None:
        store = Store(platform=Platform.SHOPEE, external_shop_id=str(shop_id), active=True)
        db.add(store)
    store.name = info.get("shop_name") or store.name or f"Shopee {shop_id}"
    store.region = info.get("region") or store.region
    apply_token_set(store, tokens)
    db.commit()
    return store
