from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import Platform
from ..models import Store
from ..platforms.registry import get_mercadolivre_client
from ..security import pkce_verifier_for
from .tokens import apply_token_set


def authorize_url(state: str) -> str:
    s = get_settings()
    return get_mercadolivre_client().authorize_url(s.mercadolivre_redirect_uri, state, pkce_verifier_for(state))


def authorize_with_code(db: Session, code: str, state: str) -> Store:
    """Troca o código por tokens e cadastra/atualiza a conta (loja) do Mercado Livre."""
    client = get_mercadolivre_client()
    tokens, user_id = client.exchange_code(code, get_settings().mercadolivre_redirect_uri, pkce_verifier_for(state))
    me = client.get_me(tokens.access_token)
    user_id = str(me.get("id") or user_id)

    store = db.scalar(
        select(Store).where(Store.platform == Platform.MERCADO_LIVRE, Store.external_shop_id == user_id)
    )
    if store is None:
        store = Store(platform=Platform.MERCADO_LIVRE, external_shop_id=user_id, active=True)
        db.add(store)
    store.name = me.get("nickname") or store.name or f"Mercado Livre {user_id}"
    store.region = me.get("site_id") or store.region
    apply_token_set(store, tokens)
    db.commit()
    return store
