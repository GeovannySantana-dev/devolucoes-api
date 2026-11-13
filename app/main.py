# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import logging
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from apscheduler.schedulers.background import BackgroundScheduler
from fastapi import Depends, FastAPI, Request
from fastapi.exception_handlers import http_exception_handler
from fastapi.openapi.docs import get_swagger_ui_html
from fastapi.responses import RedirectResponse
from fastapi.staticfiles import StaticFiles
from starlette.exceptions import HTTPException as StarletteHTTPException

from .config import get_settings
from .db import new_session
from .routers import auth_mercadolivre, auth_shein, auth_shopee, auth_tiktok, integration, reports, returns, stores, webhooks
from .routers import erp as erp_router
from .routers import jobs
from .services import erp
from .services.sync import sync_all
from .services.tokens import refresh_expiring_tokens
from .services.tracking import refresh_shopee_tracking
from .ui import routes as ui

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
# O httpx loga a URL completa, e a Shopee exige o access_token na query string
logging.getLogger("httpx").setLevel(logging.WARNING)


def _job(fn):
    def run():
        with new_session() as db:
            fn(db)

    return run


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    scheduler = None
    if settings.enable_scheduler:
        scheduler = BackgroundScheduler(timezone=settings.timezone)
        scheduler.add_job(
            _job(sync_all), "interval", minutes=settings.sync_interval_minutes, id="sync_all", max_instances=1
        )
        scheduler.add_job(_job(refresh_expiring_tokens), "interval", hours=6, id="refresh_tokens", max_instances=1)
        scheduler.add_job(
            _job(refresh_shopee_tracking), "interval", minutes=settings.sync_interval_minutes,
            id="shopee_tracking", max_instances=1,
        )
        if settings.olist_api_token:
            scheduler.add_job(
                _job(erp.link_pending), "interval", minutes=settings.sync_interval_minutes, id="olist_link", max_instances=1
            )
            scheduler.add_job(_job(erp.check_invoices), "interval", minutes=10, id="olist_invoices", max_instances=1)
            if settings.olist_import_undelivered:
                scheduler.add_job(
                    _job(erp.import_undelivered), "interval", minutes=30, id="olist_undelivered", max_instances=1
                )
        scheduler.start()
    yield
    if scheduler:
        scheduler.shutdown(wait=False)


app = FastAPI(
    title="API de Devoluções",
    description="Gestão centralizada de devoluções: TikTok Shop, Shopee, Mercado Livre e Shein.",
    version="1.0.0",
    lifespan=lifespan,
    # A documentação só abre com login nas telas (rotas /docs e /openapi.json abaixo)
    docs_url=None,
    redoc_url=None,
    openapi_url=None,
)

app.include_router(returns.router)
app.include_router(erp_router.router)
app.include_router(reports.router)
app.include_router(integration.router)
app.include_router(stores.router)
app.include_router(auth_tiktok.router)
app.include_router(auth_tiktok.router2)
app.include_router(auth_shopee.router)
app.include_router(auth_mercadolivre.router)
app.include_router(auth_shein.router)
app.include_router(webhooks.router)
app.include_router(jobs.router)
app.include_router(ui.router)
app.mount("/static", StaticFiles(directory=str(Path(__file__).parent / "static")), name="static")


@app.exception_handler(ui.LoginRequired)
def _login_required(request: Request, exc: ui.LoginRequired):
    return RedirectResponse(f"/app/login?{urlencode({'next': exc.next_url})}", status_code=303)


@app.exception_handler(ui.Forbidden)
def _forbidden(request: Request, exc: ui.Forbidden):
    return RedirectResponse("/app?erro=Seu perfil não tem permissão para essa ação", status_code=303)


@app.exception_handler(ui.NotFoundUI)
def _not_found(request: Request, exc: ui.NotFoundUI):
    return RedirectResponse("/app?erro=Registro não encontrado", status_code=303)


_RETURN_ACTION = re.compile(r"^(/app/devolucoes/[0-9a-fA-F-]{36})/")


@app.exception_handler(StarletteHTTPException)
async def _http_error(request: Request, exc: StarletteHTTPException):
    # Aba que ficou num endereço de envio (o celular fechou o Chrome no meio do POST): ao reabrir,
    # o navegador pede GET e recebia {"detail":"Method Not Allowed"}. Volta para a tela de onde veio.
    path = request.url.path
    if exc.status_code == 405 and request.method == "GET" and path.startswith("/app/"):
        m = _RETURN_ACTION.match(path)
        return RedirectResponse(m.group(1) if m else "/app", status_code=303)
    return await http_exception_handler(request, exc)


@app.middleware("http")
async def security_headers(request: Request, call_next):
    resp = await call_next(request)
    resp.headers.setdefault("X-Content-Type-Options", "nosniff")
    resp.headers.setdefault("X-Frame-Options", "DENY")
    resp.headers.setdefault("Referrer-Policy", "same-origin")
    if request.headers.get("x-forwarded-proto", "").startswith("https") or request.url.scheme == "https":
        resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000")
    return resp


@app.get("/openapi.json", include_in_schema=False)
def openapi_schema(user=Depends(ui.current_user)):
    return app.openapi()


@app.get("/docs", include_in_schema=False)
def api_docs(user=Depends(ui.current_user)):
    return get_swagger_ui_html(openapi_url="/openapi.json", title=f"{app.title} · documentação")


@app.get("/", include_in_schema=False)
def home():
    return RedirectResponse("/app", status_code=303)


@app.get("/health", tags=["Infra"])
def health():
    return {"status": "ok"}
