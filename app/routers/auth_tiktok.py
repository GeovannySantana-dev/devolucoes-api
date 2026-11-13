# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from html import escape
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, HTTPException
from fastapi.responses import HTMLResponse
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..platforms.base import PlatformError
from ..security import check_oauth_state, make_oauth_state, require_api_key
from ..services.tiktok_auth import authorize_with_code

router = APIRouter(prefix="/auth/tiktok", tags=["Autorização TikTok Shop"])
# O app é privado e só autoriza a loja do próprio parceiro: a loja de outro parceiro tem um app só dela
router2 = APIRouter(prefix="/auth/tiktok2", tags=["Autorização TikTok Shop"])


def _link(service_id: str) -> dict[str, str]:
    params = {"service_id": service_id, "state": make_oauth_state()}
    return {"url": f"{get_settings().tiktok_authorize_url}?{urlencode(params)}"}


def _finish(db: Session, code: str, state: str, app: int) -> str:
    if not check_oauth_state(state):
        raise HTTPException(400, "state inválido ou expirado; gere um novo link")

    try:
        names = [s.name for s in authorize_with_code(db, code, app)]
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc

    return f"<h2>Loja(s) autorizada(s): {escape(', '.join(names)) or 'nenhuma'}</h2><p>Pode fechar esta janela.</p>"


@router.get("/authorize-url", dependencies=[Depends(require_api_key)])
def authorize_url():
    """Gera o link que o dono da loja abre para autorizar o app. Repita para cada uma das lojas."""
    return _link(get_settings().tiktok_service_id)


@router.get("/callback", response_class=HTMLResponse)
def callback(code: str, state: str, db: Session = Depends(get_db)):
    """Redirect URL configurada no Partner Center: {PUBLIC_BASE_URL}/auth/tiktok/callback"""
    return _finish(db, code, state, 1)


@router2.get("/authorize-url", dependencies=[Depends(require_api_key)])
def authorize_url_2():
    """O mesmo link do segundo app, para a loja dele."""
    service_id = get_settings().tiktok2_service_id
    if not service_id:
        raise HTTPException(503, "Segundo app do TikTok não configurado (TIKTOK2_SERVICE_ID)")
    return _link(service_id)


@router2.get("/callback", response_class=HTMLResponse)
def callback_2(code: str, state: str, db: Session = Depends(get_db)):
    """Redirect URL do segundo app no Partner Center: {PUBLIC_BASE_URL}/auth/tiktok2/callback"""
    return _finish(db, code, state, 2)
