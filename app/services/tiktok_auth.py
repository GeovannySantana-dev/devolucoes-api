# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..enums import Platform
from ..models import Store
from ..platforms.base import PlatformError
from ..platforms.registry import get_tiktok_client
from .tokens import apply_token_set

REQUIRED_SCOPES = {"seller.authorization.info", "seller.return_refund.basic"}


def authorize_with_code(db: Session, auth_code: str, app: int = 1) -> list[Store]:
    """Troca o código de autorização por tokens e cadastra/atualiza as lojas autorizadas."""
    client = get_tiktok_client(app)
    tokens = client.exchange_code(auth_code)
    if tokens.granted_scopes is not None:
        missing = REQUIRED_SCOPES - set(tokens.granted_scopes)
        if missing:
            raise PlatformError(
                f"O token não inclui os escopos {sorted(missing)}. "
                f"Escopos concedidos: {sorted(tokens.granted_scopes)}. "
                "Desinstale o app na loja e autorize novamente para aceitar as novas permissões."
            )
    shops = client.get_authorized_shops(tokens.access_token)

    stores = []
    for shop in shops:
        store = db.scalar(
            select(Store).where(Store.platform == Platform.TIKTOK, Store.external_shop_id == str(shop["id"]))
        )
        if store is None:
            store = Store(platform=Platform.TIKTOK, external_shop_id=str(shop["id"]), active=True)
            db.add(store)
        store.name = shop.get("name") or tokens.seller_name or str(shop["id"])
        store.shop_cipher = shop.get("cipher")
        store.tiktok_app = app
        store.region = shop.get("region") or tokens.region
        apply_token_set(store, tokens)
        stores.append(store)
    db.commit()
    return stores
