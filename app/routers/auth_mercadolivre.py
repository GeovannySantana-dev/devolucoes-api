# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from html import escape

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..db import get_db
from ..platforms.base import PlatformError
from ..security import check_oauth_state, make_oauth_state, require_api_key
from ..services import mercadolivre_auth

router = APIRouter(prefix="/auth/mercadolivre", tags=["Autorização Mercado Livre"])


@router.get("/authorize-url", dependencies=[Depends(require_api_key)])
def authorize_url():
    """Link que o titular da conta abre (logado no Mercado Livre) para autorizar o app."""
    try:
        return {"url": mercadolivre_auth.authorize_url(make_oauth_state())}
    except PlatformError as exc:
        raise HTTPException(500, str(exc)) from exc


@router.get("/callback", response_class=HTMLResponse)
def callback(code: str, state: str, db: Session = Depends(get_db)):
    """MERCADOLIVRE_REDIRECT_URI deve apontar para cá quando a API estiver publicada."""
    if not check_oauth_state(state):
        raise HTTPException(400, "state inválido ou expirado; gere um novo link")
    try:
        store = mercadolivre_auth.authorize_with_code(db, code, state)
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc
    return f"<h2>Conta autorizada: {escape(store.name)}</h2><p>Pode fechar esta janela.</p>"
