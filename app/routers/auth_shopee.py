from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..platforms.base import PlatformError
from ..platforms.registry import get_shopee_client
from ..security import check_oauth_state, make_oauth_state, require_api_key
from ..services.shopee_auth import authorize_with_code

router = APIRouter(prefix="/auth/shopee", tags=["Autorização Shopee"])


@router.get("/authorize-url", dependencies=[Depends(require_api_key)])
def authorize_url():
    """Gera o link que o dono da loja abre (logado no Seller Center) para autorizar o app."""
    s = get_settings()
    redirect = f"{s.public_base_url.rstrip('/')}/auth/shopee/callback?{urlencode({'state': make_oauth_state()})}"
    try:
        return {"url": get_shopee_client().authorize_url(redirect)}
    except PlatformError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/callback", response_class=HTMLResponse)
def callback(code: str, shop_id: str, state: str, db: Session = Depends(get_db)):
    """A Shopee redireciona para cá com ?code=&shop_id= (o state vem do link gerado acima)."""
    if not check_oauth_state(state):
        raise HTTPException(400, "state inválido ou expirado; gere um novo link")
    try:
        store = authorize_with_code(db, code, shop_id)
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc
    return f"<h2>Loja autorizada: {escape(store.name)}</h2><p>Pode fechar esta janela.</p>"
