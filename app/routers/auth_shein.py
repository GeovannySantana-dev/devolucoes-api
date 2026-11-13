from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..enums import Platform
from ..models import Store
from ..platforms.base import PlatformError
from ..platforms.registry import get_shein_client
from ..security import check_oauth_state, make_oauth_state, require_api_key

router = APIRouter(prefix="/auth/shein", tags=["Autorização Shein"])


def _callback_url() -> str:
    return f"{get_settings().public_base_url.rstrip('/')}/auth/shein/callback"


@router.get("/authorize-url", dependencies=[Depends(require_api_key)])
def authorize_url():
    """Link que o admin da conta principal abre, logado no Seller Center da Shein, para autorizar o app."""
    try:
        return {"url": get_shein_client().authorize_url(_callback_url(), make_oauth_state())}
    except PlatformError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/callback", response_class=HTMLResponse)
def callback(tempToken: str, state: str = "", db: Session = Depends(get_db)):  # noqa: N803 (nome da Shein)
    """A Shein redireciona para cá com ?appid=&tempToken=&state= (tempToken vale 10 minutos)."""
    if not check_oauth_state(state):
        raise HTTPException(400, "state inválido ou expirado; gere um novo link")
    try:
        keys = get_shein_client().exchange_temp_token(tempToken)
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc
    # Uma loja Shein por openKeyId; as chaves não expiram (ficam cifradas no banco, como os tokens)
    store = db.scalar(select(Store).where(Store.platform == Platform.SHEIN, Store.external_shop_id == keys["open_key_id"]))
    if store is None:
        # Reautorização pode trazer um openKeyId novo: continua na loja Shein que já existe (uma só)
        existing = db.scalars(select(Store).where(Store.platform == Platform.SHEIN)).all()
        store = existing[0] if len(existing) == 1 else None
    if store is None:
        store = Store(platform=Platform.SHEIN, name="Shein")
        db.add(store)
    store.external_shop_id = keys["open_key_id"]
    store.access_token = keys["open_key_id"]
    store.refresh_token = keys["secret_key"]
    store.active = True  # reautorizar volta a sincronizar a loja pausada
    db.commit()
    return f"<h2>Loja Shein autorizada: {escape(store.name)}</h2><p>Pode fechar esta janela.</p>"
