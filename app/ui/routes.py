# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Telas da equipe (celular primeiro): fila, detalhe, conferência, decisão e usuários.

Páginas HTML geradas no servidor. Toda ação é um formulário que redireciona de volta (POST → GET),
com o resultado em `?ok=` / `?erro=`. O login é individual: cada ação fica no histórico com o nome.
"""

import json
import logging
import re
import uuid
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from html import escape
from pathlib import Path
from urllib.parse import urlencode
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, RedirectResponse, Response, StreamingResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import Text, and_, cast, exists, func, or_, select
from sqlalchemy.orm import Session, defer, selectinload

from ..config import get_settings
from ..db import get_db
from ..enums import ACTIONABLE_STATUSES, ReturnStatus, ReturnType
from ..erp.olist import OlistError
from ..models import RejectionReason, Return, ReturnItem, ReturnMedia, Store, TransferBatch, User
from ..platforms.base import PlatformError, PlatformNotSupported
from ..platforms.registry import get_adapter
from ..routers import reports
from ..services import (
    actions,
    alerts,
    buyers,
    api_keys,
    dashboard,
    erp,
    excel,
    inspection,
    losses,
    media,
    media_backup,
    product_costs,
    productivity,
    store_status,
    transfers,
)
from ..services.sync import upsert_return
from ..services.users import (
    ROLES,
    SESSION_COOKIE,
    AuthError,
    RESET_MINUTES,
    authenticate,
    can,
    change_own_password,
    create_user,
    end_all_sessions,
    find_active_user,
    make_reset_token,
    make_session,
    set_password,
    user_from_reset_token,
    user_from_session,
)
from . import labels

log = logging.getLogger(__name__)
router = APIRouter(prefix="/app", include_in_schema=False)
templates = Jinja2Templates(directory=str(Path(__file__).resolve().parents[1] / "templates"))
labels.register(templates.env)

PAGE_SIZE = 30
# Devoluções físicas a caminho ou entregues, ainda não conferidas, solicitadas nos últimos N dias
TO_INSPECT_DAYS = 30
SHOPEE_INSPECT_DAYS = 21
NOT_INSPECTABLE = (ReturnStatus.CANCELLED, ReturnStatus.REJECTED)

TABS = {
    "decidir": "Aguardando decisão",
    "conferir": "Para conferir",
    "vencidas": "Vencidas",
    "erros": "Erros no Olist",
    "todas": "Todas",
}


class LoginRequired(Exception):
    def __init__(self, next_url: str = "/app"):
        self.next_url = next_url


class Forbidden(Exception):
    pass


class NotFoundUI(Exception):
    pass


def current_user(request: Request, db: Session = Depends(get_db)) -> User:
    user = user_from_session(db, request.cookies.get(SESSION_COOKIE))
    if not user:
        # Mantém a busca (ex.: código bipado) para continuar depois do login
        target = request.url.path + (f"?{request.url.query}" if request.url.query and request.method == "GET" else "")
        raise LoginRequired(target)
    return user


def _require(user: User, permission: str) -> None:
    if not can(user, permission):
        raise Forbidden()


def _back(url: str, ok: str | None = None, erro: str | None = None) -> RedirectResponse:
    params = {k: v for k, v in (("ok", ok), ("erro", erro)) if v}
    return RedirectResponse(f"{url}?{urlencode(params)}" if params else url, status_code=303)


def _render(request: Request, name: str, user: User | None, **ctx) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        name,
        {
            "user": user,
            "can": lambda p: can(user, p),
            "ok": request.query_params.get("ok"),
            "erro": request.query_params.get("erro"),
            "now": datetime.now(UTC),
            **ctx,
        },
    )


def _load(db: Session, return_id: uuid.UUID) -> Return:
    ret = db.scalar(
        select(Return)
        .where(Return.id == return_id)
        .options(
            selectinload(Return.items),
            selectinload(Return.events),
            selectinload(Return.media),
            selectinload(Return.store),
        )
    )
    if not ret:
        raise NotFoundUI()
    return ret


def _detail_url(ret: Return) -> str:
    return f"/app/devolucoes/{ret.id}"


# ---------- login ----------

# Com o app aberto na internet, bloqueia o e-mail depois de várias senhas erradas seguidas (em memória)
LOGIN_MAX_FAILS = 5
LOGIN_WINDOW_MIN = 15
_LOGIN_FAILS: dict[str, list[datetime]] = {}


def _login_blocked(key: str) -> bool:
    cutoff = datetime.now(UTC) - timedelta(minutes=LOGIN_WINDOW_MIN)
    recent = [t for t in _LOGIN_FAILS.get(key, []) if t > cutoff]
    if recent:
        _LOGIN_FAILS[key] = recent
    else:
        _LOGIN_FAILS.pop(key, None)
    return len(recent) >= LOGIN_MAX_FAILS


@router.get("/login")
def login_page(request: Request, next: str = "/app"):
    return _render(request, "login.html", None, next=next)


@router.post("/login")
def login(
    request: Request,
    email: str = Form(...),
    senha: str = Form(...),
    next: str = Form("/app"),
    db: Session = Depends(get_db),
):
    key = email.strip().lower()
    if _login_blocked(key):
        erro = f"Muitas tentativas com senha errada. Aguarde {LOGIN_WINDOW_MIN} minutos e tente de novo."
        return _render(request, "login.html", None, next=next, erro=erro, email=email)
    try:
        user = authenticate(db, email, senha)
    except AuthError as exc:
        _LOGIN_FAILS.setdefault(key, []).append(datetime.now(UTC))
        return _render(request, "login.html", None, next=next, erro=str(exc), email=email)
    _LOGIN_FAILS.pop(key, None)
    target = next if next.startswith(("/app", "/docs")) and not next.startswith("//") else "/app"
    return _session_cookie(RedirectResponse(target, status_code=303), request, user)


def _session_cookie(resp: RedirectResponse, request: Request, user: User) -> RedirectResponse:
    resp.set_cookie(
        SESSION_COOKIE,
        make_session(user),
        max_age=get_settings().session_days * 86400,
        httponly=True,
        samesite="lax",
        secure=request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").startswith("https"),
    )
    return resp


# ---------- senha: esqueci (link por e-mail) e trocar a minha ----------

RESET_EVERY = timedelta(minutes=2)  # no máximo um e-mail a cada 2 min para o mesmo endereço
_RESET_SENT: dict[str, datetime] = {}
RESET_DONE = "Se esse e-mail tiver usuário ativo, enviamos um link para criar a senha nova. Ele vale 30 minutos."


def _reset_email(request: Request, user: User, link: str) -> None:
    s = get_settings()
    subject = "Central de Devoluções: criar uma senha nova"
    text = (f"Olá, {user.name}.\n\nPara criar uma senha nova, abra o link abaixo (vale {RESET_MINUTES} minutos):\n{link}\n\n"
            "Se não foi você que pediu, ignore este e-mail: a sua senha continua a mesma.\n")
    html = (f"<p>Olá, {escape(user.name)}.</p><p>Para criar uma senha nova, toque no botão (vale {RESET_MINUTES} minutos):</p>"
            f'<p><a href="{escape(link)}" style="display:inline-block;background:#0f6e56;color:#fff;padding:10px 16px;'
            'border-radius:8px;text-decoration:none;font-weight:700">Criar senha nova</a></p>'
            "<p style=\"color:#6b645e\">Se não foi você que pediu, ignore este e-mail: a sua senha continua a mesma.</p>")
    alerts.send_email(s, alerts.Message(subject, text, html), [user.email])


@router.get("/esqueci-senha")
def forgot_page(request: Request, email: str = ""):
    return _render(request, "esqueci_senha.html", None, email=email)


@router.post("/esqueci-senha")
def forgot_send(request: Request, email: str = Form(...), db: Session = Depends(get_db)):
    key = email.strip().lower()
    last = _RESET_SENT.get(key)
    user = find_active_user(db, key)
    if user and not (last and datetime.now(UTC) - last < RESET_EVERY):
        base = (get_settings().app_url or get_settings().public_base_url or str(request.base_url)).rstrip("/")
        link = f"{base}/app/nova-senha?t={make_reset_token(user)}"
        try:
            _reset_email(request, user, link)
        except alerts.AlertError:
            return _render(request, "esqueci_senha.html", None, email=email,
                           erro="Não consegui enviar o e-mail agora. Peça a troca ao gestor em Usuários.")
        _RESET_SENT[key] = datetime.now(UTC)
    # Mesma resposta exista ou não o usuário (não revela quais e-mails têm cadastro)
    return _back("/app/login", ok=RESET_DONE)


@router.get("/nova-senha")
def reset_page(request: Request, t: str = "", db: Session = Depends(get_db)):
    if not user_from_reset_token(db, t):
        return _back("/app/esqueci-senha", erro="Esse link já foi usado ou venceu. Peça um novo.")
    return _render(request, "nova_senha.html", None, token=t)


@router.post("/nova-senha")
def reset_save(request: Request, t: str = Form(...), senha: str = Form(...), repetir: str = Form(...),
               db: Session = Depends(get_db)):
    user = user_from_reset_token(db, t)
    if not user:
        return _back("/app/esqueci-senha", erro="Esse link já foi usado ou venceu. Peça um novo.")
    if senha != repetir:
        return _render(request, "nova_senha.html", None, token=t, erro="As duas senhas não são iguais")
    try:
        set_password(db, user, senha)
    except AuthError as exc:
        return _render(request, "nova_senha.html", None, token=t, erro=str(exc))
    _LOGIN_FAILS.pop(user.email.lower(), None)
    return _session_cookie(_back("/app", ok="Senha nova criada. Você já está conectado."), request, user)


@router.get("/minha-senha")
def my_password_page(request: Request, user: User = Depends(current_user)):
    return _render(request, "minha_senha.html", user)


@router.post("/minha-senha")
def my_password_save(request: Request, atual: str = Form(...), senha: str = Form(...), repetir: str = Form(...),
                     user: User = Depends(current_user), db: Session = Depends(get_db)):
    try:
        change_own_password(db, user, atual, senha, repetir)
    except AuthError as exc:
        return _render(request, "minha_senha.html", user, erro=str(exc))
    # Trocar a senha derruba as sessões antigas: renova a deste aparelho
    return _session_cookie(_back("/app", ok="Senha trocada. Nos outros aparelhos, entre de novo com a senha nova."), request, user)


@router.post("/sair-de-todos")
def logout_everywhere(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Celular perdido ou emprestado: derruba as sessões de todos os aparelhos e renova a deste."""
    end_all_sessions(db, user)
    return _session_cookie(_back("/app/minha-senha", ok="Pronto: nos outros aparelhos é preciso entrar de novo."), request, user)


@router.post("/sair")
def logout():
    resp = RedirectResponse("/app/login", status_code=303)
    resp.delete_cookie(SESSION_COOKIE)
    return resp


# ---------- fila ----------

def _tab_conditions(tab: str) -> list:
    now = datetime.now(UTC)
    if tab == "decidir":
        # No TikTok, "a caminho" também aceita ação, mas a loja só decide quando o pacote chega: fica em "conferir"
        return [Return.status.in_((ReturnStatus.PENDING_REVIEW, ReturnStatus.RECEIVED))]
    if tab == "vencidas":
        return [Return.status.in_(ACTIONABLE_STATUSES), Return.action_deadline < now]
    if tab == "conferir":
        return [
            Return.received_at.is_(None),
            Return.handled_outside_at.is_(None),
            Return.return_type != ReturnType.REFUND_ONLY,
            Return.requested_at >= now - timedelta(days=TO_INSPECT_DAYS),
            or_(
                Return.status.in_((ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED, ReturnStatus.RETURNING_TO_SELLER)),
                # Shopee aceita antes de o pacote chegar: vale o rastreio reverso (app/services/tracking.py)
                (Return.platform == "shopee")
                & (Return.status == ReturnStatus.COMPLETED)
                & or_(
                    Return.logistics_status.in_(("in_transit", "delivered", "failed")),
                    # ainda não consultado: últimos dias
                    Return.logistics_status.is_(None)
                    & (Return.requested_at >= now - timedelta(days=SHOPEE_INSPECT_DAYS)),
                ),
                # Mercado Livre: reembolsada com o pacote já postado ou entregue
                (Return.platform == "mercado_livre")
                & (Return.status == ReturnStatus.COMPLETED)
                & or_(
                    Return.platform_status.ilike("%return:shipped%"),
                    Return.platform_status.ilike("%return:delivered%"),
                ),
            ),
        ]
    if tab == "erros":
        return [Return.erp_last_error.is_not(None)]
    return []


MIN_PARTIAL = 5  # etiqueta rasurada: a parte legível do código já serve
MIN_RAW = 8  # códigos longos (rastreio dos Correios, AP...BR, etc.) também são procurados nos dados do marketplace


GTIN_RE = re.compile(r"^(789|790)\d{10}$")  # código de barras da etiqueta da peça (EAN-13 do Brasil)
CEP_RE = re.compile(r"^\d{8}$")


_GTIN_CACHE: dict[str, str | None] = {}


def _sku_from_barcode(gtin: str) -> str | None:
    """EAN da etiqueta da peça -> SKU, pelo cadastro de produtos do Olist (fica guardado na memória)."""
    if gtin not in _GTIN_CACHE:
        try:
            _GTIN_CACHE[gtin] = erp.get_olist_client().find_sku_by_gtin(gtin)
        except OlistError:
            return None  # Olist fora: segue com a busca normal
    return _GTIN_CACHE[gtin]


def read_scan(q: str) -> str:
    """O que o leitor mandou -> o código a procurar.

    - QR code do Mercado Livre: {"id":"40000000001","t":"lm"} -> 40000000001 (id do envio)
    - Etiquetas com separador: 14600000000001$000101001000 -> 14600000000001
    """
    q = q.strip()
    if q.startswith("{") and '"id"' in q:
        try:
            data = json.loads(q)
            if isinstance(data, dict) and data.get("id"):
                return str(data["id"])
        except ValueError:
            pass
    if "$" in q:
        q = q.split("$", 1)[0]
    return q.strip()


def _search_conditions(q: str, broad: bool = True) -> list:
    """Busca da fila: nº da devolução/pedido, rastreio (de volta ou de ida), chave da NF, SKU ou nome do produto.

    O leitor de código de barras pode mandar o código com espaços/traços; a busca ignora isso. Com parte do
    código (etiqueta rasurada), procura também por trechos. broad=False usa só as comparações exatas (rápidas,
    com índice); a fila tenta assim primeiro e só faz a busca ampla quando não acha nada.
    """
    q = read_scan(q)
    code = re.sub(r"[\s\-./]", "", q).upper()
    like = f"%{q}%"
    sku_match = exists().where(
        ReturnItem.return_id == Return.id,
        or_(ReturnItem.seller_sku.ilike(like), ReturnItem.product_name.ilike(like)),
    )
    exact_sku = exists().where(ReturnItem.return_id == Return.id, func.upper(ReturnItem.seller_sku) == q.upper())
    conds = [
        Return.external_return_id == q,
        Return.external_order_id == q,
        Return.erp_order_number == q,
        func.upper(Return.tracking_number) == code,
        func.upper(Return.sale_tracking_number) == code,  # etiqueta do envio da venda
        exact_sku,
    ]
    if code.isdigit() and len(code) == 44:  # chave de acesso da NF de venda (DANFE dentro do pacote)
        conds.append(Return.erp_sale_invoice_key == code)
    if not broad:
        return [or_(*conds)]
    conds.append(sku_match)
    if len(code) >= MIN_PARTIAL and code.isalnum():
        part = f"%{code}%"
        conds += [
            func.upper(Return.tracking_number).like(part),
            func.upper(Return.external_order_id).like(part),
            func.upper(Return.external_return_id).like(part),
        ]
    if len(code) >= MIN_RAW and code.isalnum():
        # Rastreio que só aparece nos dados da plataforma (ex.: código de postagem, etiqueta de ida)
        conds.append(func.upper(cast(Return.raw, Text)).like(f"%{code}%"))
    return [or_(*conds)]


def _counts(db: Session) -> dict[str, int]:
    tabs = ("decidir", "conferir", "vencidas", "erros")
    # Uma consulta só para os quatro contadores (cada ida ao banco custa ~50 ms)
    row = db.execute(select(*(
        func.count().filter(and_(*_tab_conditions(tab))).label(tab) for tab in tabs
    )).select_from(Return)).one()
    return {tab: getattr(row, tab) or 0 for tab in tabs}


@router.get("")
def queue(
    request: Request,
    aba: str = "decidir",
    q: str = "",
    plataforma: str = "",
    pagina: int = 1,
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    aba = aba if aba in TABS else "decidir"
    platform_cond = [Return.platform == plataforma] if plataforma in labels.PLATFORM else []
    hint = None
    if q.strip() and pagina <= 1 and not request.headers.get("X-Live-Refresh"):
        productivity.log_event(db, user, "scan", detail=q.strip())  # bipagem (a atualização automática não conta)
    if q.strip():
        # A busca procura em tudo, independentemente da aba. Primeiro só o exato (rápido); se não achar, a ampla.
        code = re.sub(r"[\s\-./]", "", read_scan(q)).upper()
        conds = _search_conditions(q, broad=False) + platform_cond
        if not db.scalar(select(func.count()).select_from(Return).where(*conds)):
            sku = _sku_from_barcode(code) if GTIN_RE.match(code) else None
            if sku:  # etiqueta da peça: procura pelo SKU que o Olist devolveu
                conds = _search_conditions(sku, broad=False) + platform_cond
                hint = f"Código de barras da peça: {sku}"
            else:
                conds = _search_conditions(q) + platform_cond
            if CEP_RE.match(code):
                hint = ("Esse código parece um CEP (o código de barras do endereço). "
                        "Bipe o código de rastreio da etiqueta, normalmente o maior.")
    else:
        conds = _tab_conditions(aba) + platform_cond

    if aba in ("decidir", "vencidas") and not q:
        order = [Return.action_deadline.asc().nulls_last(), Return.requested_at.desc()]
    elif aba == "conferir" and not q:
        # Pacotes já entregues primeiro, depois os que vencem antes
        order = [
            ((Return.status == ReturnStatus.RECEIVED) | (Return.logistics_status == "delivered")).desc(),
            Return.action_deadline.asc().nulls_last(),
            Return.requested_at.desc(),
        ]
    elif q:
        # Busca (ex.: SKU da etiqueta da peça quando a do pacote está rasurada): as que ainda faltam conferir
        # aparecem primeiro, com as já entregues no topo
        pending = (
            Return.received_at.is_(None)
            & Return.handled_outside_at.is_(None)
            & (Return.return_type != ReturnType.REFUND_ONLY)
            & Return.status.notin_((ReturnStatus.CANCELLED, ReturnStatus.REJECTED))
        )
        order = [
            pending.desc(),
            ((Return.status == ReturnStatus.RECEIVED) | (Return.logistics_status == "delivered")).desc(),
            Return.requested_at.desc().nulls_last(),
        ]
    else:
        order = [Return.requested_at.desc().nulls_last()]

    total = db.scalar(select(func.count()).select_from(Return).where(*conds)) or 0
    if q.strip() and not total:
        log.info("Busca sem resultado: %r", q[:120])
    rows = db.scalars(
        select(Return)
        .where(*conds)
        # O cartão da fila não usa os dados brutos da plataforma (e a fila recarrega a cada 30 s)
        .options(defer(Return.raw), selectinload(Return.items).defer(ReturnItem.raw), selectinload(Return.store))
        .order_by(*order)
        .offset((max(pagina, 1) - 1) * PAGE_SIZE)
        .limit(PAGE_SIZE)
    ).all()

    # Leitura do código de rastreio: um resultado só já abre a devolução
    if q.strip() and total == 1:
        return RedirectResponse(_detail_url(rows[0]), status_code=303)

    return _render(
        request,
        "fila.html",
        user,
        tabs=TABS,
        aba=aba,
        q=q,
        plataforma=plataforma,
        platforms=labels.PLATFORM,
        rows=rows,
        total=total,
        hint=hint,
        pagina=max(pagina, 1),
        page_size=PAGE_SIZE,
        counts=_counts(db),
        shift=productivity.shift_state(db, user) if can(user, "inspect") else None,
        store_alerts=store_status.stopped(db) if can(user, "users") else [],
        buyer_hist=buyers.history_counts(db, {r.buyer_key for r in rows if r.buyer_key}),
    )


# ---------- detalhe ----------

def _contest_open(db: Session, ret: Return) -> bool:
    """A plataforma ainda oferece motivo para contestar? Se a consulta falhar, mantém o botão (a tela de recusa avisa)."""
    try:
        return bool(get_adapter(db, ret.store).reject_reasons(ret.store, ret))
    except Exception as exc:  # não pode impedir a devolução de abrir
        log.warning("Motivos de contestação de %s indisponíveis: %s", ret.external_return_id, exc)
        return True


@router.get("/devolucoes/{return_id}")
def detail(request: Request, return_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ret = _load(db, return_id)
    physical = ret.return_type != ReturnType.REFUND_ONLY
    can_inspect = (
        physical and not ret.received_at and not ret.handled_outside_at and ret.status not in NOT_INSPECTABLE
    )
    if can_inspect and can(user, "inspect"):
        # Câmera da bancada: o vídeo da conferência começa quando o operador abre a devolução (após bipar)
        ret.inspection_started_at = datetime.now(UTC)
        productivity.log_event(db, user, "open", return_id=ret.id, commit=False)
        db.commit()
    contestable = (
        ret.status not in ACTIONABLE_STATUSES and actions.can_contest_after_accept(ret) and ret.decision != "rejected"
    )
    # Só pergunta à plataforma quando o botão de contestar apareceria para esta pessoa
    contest_open = not (contestable and can(user, "decide")) or _contest_open(db, ret)
    return _render(
        request,
        "detalhe.html",
        user,
        ret=ret,
        physical=physical,
        can_inspect=can_inspect,
        can_decide=ret.status in ACTIONABLE_STATUSES and ret.platform != "shein",  # Shein já aceita sozinha
        can_contest=contestable and contest_open,
        contest_closed=contestable and not contest_open,
        buyer_media=media.buyer_media(ret),
        buyer_comment=erp.buyer_comment(ret),
        invoice_mode=get_settings().olist_invoice_mode,
        conditions=labels.CONDITION,
        events=list(reversed(ret.events)),
        same_buyer=buyers.others(db, ret),
    )


def _save_uploads(db: Session, ret: Return, files: list[UploadFile], actor: str) -> list[ReturnMedia]:
    return [
        media.save_media(db, ret, f.file, f.filename, f.content_type, actor)
        for f in files
        if f and f.filename
    ]


@router.post("/devolucoes/{return_id}/conferir")
def inspect(
    return_id: uuid.UUID,
    condicao: str = Form(...),
    observacoes: str = Form(""),
    gerar_nf: bool = Form(False),
    enviados: int = Form(0),
    motivo_defeito: str = Form(""),
    arquivos: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require(user, "inspect")
    ret = _load(db, return_id)
    if condicao not in labels.CONDITION:
        return _back(_detail_url(ret), erro="Escolha revenda ou defeito")
    notes = observacoes.strip() or None
    reason = motivo_defeito or None
    try:
        # as fotos sobem antes: a conferência de defeito exige pelo menos uma
        n = len(_save_uploads(db, ret, arquivos, user.actor)) + int(enviados or 0)
        if condicao == "defect":
            inspection.check_defect_evidence(db, ret, reason, notes)
        inspection.receive_return(db, ret, user.actor, condicao, notes, gerar_nf, defect_reason=reason)
    except (inspection.InspectionError, media.MediaError) as exc:
        db.rollback()
        return _back(_detail_url(ret), erro=str(exc))
    productivity.log_event(db, user, "inspect", return_id=ret.id)
    msg = "Conferência registrada" + (f" com {n} arquivo(s)" if n else "")
    if ret.erp_last_error:
        return _back(_detail_url(ret), ok=msg, erro=f"Olist: {ret.erp_last_error}")
    return _back(_detail_url(ret), ok=msg)


@router.post("/devolucoes/{return_id}/midias")
def upload_media(
    request: Request,
    return_id: uuid.UUID,
    arquivos: list[UploadFile] = File(default=[]),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require(user, "media")
    ret = _load(db, return_id)
    # Envio em segundo plano (app.js): o vídeo sobe enquanto a pessoa termina a conferência
    background = request.headers.get("x-background-upload") == "1"
    try:
        saved = _save_uploads(db, ret, arquivos, user.actor)
    except media.MediaError as exc:
        db.rollback()
        if background:
            return JSONResponse({"erro": str(exc)}, status_code=400)
        return _back(_detail_url(ret), erro=str(exc))
    if background:
        return JSONResponse({"ids": [str(m.id) for m in saved]})
    n = len(saved)
    return _back(_detail_url(ret), ok=f"{n} arquivo(s) enviado(s)" if n else None, erro=None if n else "Nenhum arquivo")


@router.post("/devolucoes/{return_id}/midias/{media_id}/excluir")
def remove_media(
    return_id: uuid.UUID, media_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _require(user, "media")
    ret = _load(db, return_id)
    item = db.get(ReturnMedia, media_id)
    if item and item.return_id == ret.id:
        media.delete_media(db, ret, item, user.actor)
    return _back(_detail_url(ret), ok="Arquivo removido")


# Arquivo enviado pela equipe: o navegador nunca executa nada dele, mesmo aberto direto
_MEDIA_HEADERS = {"Content-Security-Policy": "sandbox"}


@router.get("/midias/{media_id}")
def get_media(media_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    item = db.get(ReturnMedia, media_id)
    if not item:
        raise NotFoundUI()
    storage = media.get_storage()
    if isinstance(storage, media.LocalStorage):
        path = storage.path(item.storage_path)
        if not path.exists():
            raise NotFoundUI()
        # FileResponse aceita Range: o vídeo pode ser avançado no player
        return FileResponse(path, media_type=item.content_type, filename=item.original_name, headers=_MEDIA_HEADERS)
    chunks = storage.iter_chunks(item.storage_path)
    if chunks is None:
        raise NotFoundUI()
    return StreamingResponse(
        chunks,
        media_type=item.content_type,
        headers={"Cache-Control": "private, max-age=86400", **_MEDIA_HEADERS},
    )


@router.post("/devolucoes/{return_id}/condicao")
def change_condition(
    return_id: uuid.UUID, condicao: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _require(user, "inspect")
    ret = _load(db, return_id)
    try:
        inspection.set_condition(db, ret, user.actor, condicao)
    except inspection.InspectionError as exc:
        return _back(_detail_url(ret), erro=str(exc))
    return _back(_detail_url(ret), ok="Condição alterada")


@router.post("/devolucoes/{return_id}/nota")
def note(return_id: uuid.UUID, nota: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "note")
    ret = _load(db, return_id)
    if nota.strip():
        inspection.add_note(db, ret, user.actor, nota.strip())
    return _back(_detail_url(ret), ok="Nota salva")


@router.post("/devolucoes/{return_id}/atualizar")
def refresh(return_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    ret = _load(db, return_id)
    try:
        nr = get_adapter(db, ret.store).fetch_return(ret.store, ret.external_return_id)
        if nr:
            upsert_return(db, ret.store, nr, source="api")
            db.commit()
    except (PlatformError, PlatformNotSupported) as exc:
        return _back(_detail_url(ret), erro=str(exc))
    return _back(_detail_url(ret), ok="Dados atualizados da plataforma")


# ---------- decisão no marketplace ----------

@router.post("/devolucoes/{return_id}/aceitar")
def accept(
    return_id: uuid.UUID, nota: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _require(user, "decide")
    ret = _load(db, return_id)
    try:
        actions.approve(db, ret, user.actor, nota.strip() or None, None)
    except (actions.ActionError, PlatformError, PlatformNotSupported) as exc:
        return _back(_detail_url(ret), erro=f"Não foi possível aceitar: {exc}")
    return _back(_detail_url(ret), ok="Devolução aceita no marketplace")


@router.get("/devolucoes/{return_id}/recusar")
def reject_page(
    request: Request,
    return_id: uuid.UUID,
    motivo_interno: str = "",
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require(user, "decide")
    ret = _load(db, return_id)
    try:
        reasons = get_adapter(db, ret.store).reject_reasons(ret.store, ret)
    except (PlatformError, PlatformNotSupported) as exc:
        return _back(_detail_url(ret), erro=f"Não foi possível buscar os motivos: {exc}")
    internal = db.scalars(
        select(RejectionReason).where(RejectionReason.active).order_by(RejectionReason.label)
    ).all()
    if not motivo_interno and ret.item_condition == "mismatch":
        motivo_interno = "item_divergente"
    return _render(
        request, "recusar.html", user, ret=ret, reasons=reasons, internal=internal, preselect=motivo_interno
    )


@router.post("/devolucoes/{return_id}/recusar")
def reject(
    return_id: uuid.UUID,
    motivo_plataforma: str = Form(...),
    motivo_interno: str = Form(""),
    comentario: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require(user, "decide")
    ret = _load(db, return_id)
    try:
        actions.reject(
            db, ret, user.actor, motivo_plataforma, motivo_interno or None, comentario.strip() or None, None
        )
    except (actions.ActionError, PlatformError, PlatformNotSupported) as exc:
        return _back(f"{_detail_url(ret)}/recusar", erro=f"Não foi possível recusar: {exc}")
    return _back(_detail_url(ret), ok="Devolução recusada no marketplace", erro=getattr(ret, "reject_warning", None))


# ---------- Olist ----------

@router.post("/devolucoes/{return_id}/olist/{acao}")
def olist_action(return_id: uuid.UUID, acao: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    ret = _load(db, return_id)
    steps = {
        "vincular": (lambda: erp.link_return(db, ret), "Pedido do Olist vinculado"),
        "gerar-nf": (lambda: erp.create_return_invoice(db, ret, user.actor), "NF de devolução criada no Olist"),
        "atualizar-nf": (lambda: erp.refresh_invoice_status(db, ret), "Situação da NF atualizada"),
    }
    if acao not in steps:
        return _back(_detail_url(ret), erro="Ação inválida")
    fn, msg = steps[acao]
    try:
        fn()
    except (erp.ErpError, OlistError) as exc:
        db.rollback()
        return _back(_detail_url(ret), erro=f"Olist: {exc}")
    return _back(_detail_url(ret), ok=msg)


@router.get("/ajuda")
def help_page(request: Request, user: User = Depends(current_user)):
    return _render(request, "ajuda.html", user)


# ---------- lotes de transferência SP -> MS (peças de revenda) ----------

@router.get("/lotes")
def batches_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    ready = transfers.eligible(db)

    def recent(direction):
        return db.scalars(select(TransferBatch).where(TransferBatch.direction == direction, TransferBatch.status != "open")
                          .order_by(TransferBatch.number.desc()).limit(30)).all()

    back = transfers.current_back_batch(db)
    in_ms = transfers.in_ms(db)
    return _render(request, "lotes.html", user, ready=ready, rows=transfers.summarize(ready),
                   waiting=transfers.waiting_invoice(db), batches=recent(transfers.TO_MS),
                   back=back, back_rows=transfers.summarize(list(back.back_returns)) if back else [],
                   in_ms_rows=transfers.summarize(in_ms), back_batches=recent(transfers.TO_SP),
                   filial_ok=bool(get_settings().olist_filial_api_token))


@router.post("/lotes/fechar")
def batches_close(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    try:
        batch = transfers.close_batch(db, user.actor)
    except transfers.TransferError as exc:
        return _back("/app/lotes", erro=str(exc))
    return _back("/app/lotes", ok=f"Lote {batch.number} fechado com {batch.pieces} peças. Emita a NF de transferência "
                                  "no Olist e informe o número no lote.")


def _batch(db: Session, batch_id: uuid.UUID) -> TransferBatch:
    batch = db.get(TransferBatch, batch_id)
    if not batch:
        raise NotFoundUI()
    return batch


MONTHS = ("janeiro", "fevereiro", "março", "abril", "maio", "junho", "julho", "agosto", "setembro", "outubro",
          "novembro", "dezembro")


def _losses(db: Session, mes: str):
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    first, start, end = losses.month_bounds(mes, today)
    rows = losses.collect(db, start, end)
    return first, today, rows


@router.get("/perdas")
def losses_page(request: Request, mes: str = "", modelo: str = "mes", user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    """Perdas do mês: peças com defeito (descarte), base da NF de baixa por perda, CFOP 5.927."""
    _require(user, "reports")
    first, today, rows = _losses(db, mes)
    # Perda por modelo: o mês da tela ou os 6 meses que terminam nele
    since = losses.month_bounds(losses.shift_month(first, -5) if modelo == "6m" else f"{first:%Y-%m}", today)[1]
    until = losses.month_bounds(f"{first:%Y-%m}", today)[2]
    return _render(request, "perdas.html", user, rows=rows, s=losses.summarize(rows), first=first,
                   models=losses.by_model(db, since, until), modelo="6m" if modelo == "6m" else "mes",
                   month_label=f"{MONTHS[first.month - 1]}/{first.year}", mes=f"{first:%Y-%m}",
                   prev=losses.shift_month(first, -1),
                   next=losses.shift_month(first, 1) if first < today.replace(day=1) else None)


@router.get("/perdas/excel")
def losses_excel(mes: str = "", user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "reports")
    first, _, rows = _losses(db, mes)
    content = excel.build_losses_xlsx(f"{first:%m-%Y}", rows, losses.by_sku(rows))
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="perdas-{first:%Y-%m}.xlsx"'})


@router.get("/lotes/{batch_id}/excel")
def batches_excel(batch_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    rets = list(batch.back_returns if batch.direction == transfers.TO_SP else batch.returns)
    content = excel.build_transfer_xlsx(batch.number, transfers.summarize(rets), rets)
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="lote-transferencia-{batch.number}.xlsx"'})


@router.post("/lotes/{batch_id}/nf")
def batches_invoice(batch_id: uuid.UUID, numero: str = Form(...), user: User = Depends(current_user),
                    db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        transfers.set_invoice(db, batch, numero)
    except transfers.TransferError as exc:
        return _back("/app/lotes", erro=str(exc))
    return _back("/app/lotes", ok=f"NF {batch.erp_invoice_number} registrada no lote {batch.number}")


@router.post("/lotes/{batch_id}/estoque-ms")
def batches_stock(batch_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        result = transfers.post_ms_stock(db, batch)
    except transfers.TransferError as exc:
        return _back("/app/lotes", erro=str(exc))
    if result["errors"]:
        return _back("/app/lotes", erro=batch.last_error)
    return _back("/app/lotes", ok=f"Lote {batch.number}: {len(result['ok'])} SKUs com entrada no estoque do MS")


# ---------- lote de volta MS -> SP (peças de devolução que voltam para venda) ----------

def _back_lotes(ok: str | None = None, erro: str | None = None) -> RedirectResponse:
    resp = _back("/app/lotes", ok=ok, erro=erro)
    resp.headers["location"] += "#volta"
    return resp


@router.post("/lotes/volta/abrir")
def back_open(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = transfers.open_back_batch(db, user.actor)
    return _back_lotes(ok=f"Lote de volta {batch.number} aberto: bipe as peças")


@router.post("/lotes/volta/{batch_id}/bipar")
def back_scan(batch_id: uuid.UUID, codigo: str = Form(""), user: User = Depends(current_user),
              db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        ret = transfers.scan(db, batch, codigo)
    except transfers.TransferError as exc:
        return _back_lotes(erro=str(exc))
    sku = ", ".join((i.seller_sku or "") for i in ret.items)
    return _back_lotes(ok=f"{sku} bipada (pedido {ret.external_order_id}) · {batch.pieces} peças no lote")


@router.post("/lotes/volta/{batch_id}/remover")
def back_unscan(batch_id: uuid.UUID, devolucao: str = Form(...), user: User = Depends(current_user),
                db: Session = Depends(get_db)):
    _require(user, "erp")
    try:
        transfers.unscan(db, _batch(db, batch_id), devolucao)
    except transfers.TransferError as exc:
        return _back_lotes(erro=str(exc))
    return _back_lotes(ok="Peça tirada do lote")


@router.post("/lotes/volta/{batch_id}/fechar")
def back_close(batch_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        transfers.close_back_batch(db, batch, user.actor)
    except transfers.TransferError as exc:
        return _back_lotes(erro=str(exc))
    return _back_lotes(ok=f"Lote de volta {batch.number} fechado com {batch.pieces} peças. Emita a NF de "
                          "transferência na conta 2 do Olist, sem lançar estoque, e informe o número.")


@router.post("/lotes/volta/{batch_id}/nf")
def back_invoice(batch_id: uuid.UUID, numero: str = Form(...), user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        transfers.set_invoice(db, batch, numero)
    except transfers.TransferError as exc:
        return _back_lotes(erro=str(exc))
    return _back_lotes(ok=f"NF {batch.erp_invoice_number} registrada no lote de volta {batch.number}")


@router.post("/lotes/volta/{batch_id}/baixar-ms")
def back_ship(batch_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        transfers.ship_from_ms(db, batch)
    except transfers.TransferError as exc:
        return _back_lotes(erro=str(exc))
    if batch.status == "error":
        return _back_lotes(erro=batch.last_error)
    return _back_lotes(ok=f"Lote de volta {batch.number}: {batch.pieces} peças baixadas do estoque do MS")


@router.post("/lotes/volta/{batch_id}/entrada-sp")
def back_receive(batch_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "erp")
    batch = _batch(db, batch_id)
    try:
        transfers.receive_in_sp(db, batch, actor=user.actor)
    except transfers.TransferError as exc:
        return _back_lotes(erro=str(exc))
    if batch.status == "error":
        return _back_lotes(erro=batch.last_error)
    return _back_lotes(ok=f"Lote de volta {batch.number}: {batch.pieces} peças de volta à venda em SP")


# ---------- limpeza da fila (gestor): devoluções tratadas no processo antigo ----------

def _cutoff(ate: str) -> tuple[date, datetime]:
    """'2026-10-15' -> (data, início do dia seguinte no fuso local): inclui o dia escolhido."""
    day = date.fromisoformat(ate)
    tz = ZoneInfo(get_settings().timezone)
    return day, datetime.combine(day + timedelta(days=1), datetime.min.time(), tz)


@router.get("/limpeza")
def cleanup_page(request: Request, ate: str = "", fechadas: str = "", user: User = Depends(current_user),
                 db: Session = Depends(get_db)):
    _require(user, "users")
    preview, day, kept = None, None, 0
    only_closed = fechadas == "1" or not ate  # padrão: só as já encerradas
    if ate:
        try:
            day, cutoff = _cutoff(ate)
        except ValueError:
            return _back("/app/limpeza", erro="Data inválida")
        preview = inspection.handled_outside_preview(db, cutoff, only_closed)
        if only_closed:  # quantas ficam na fila por ainda estarem em andamento
            kept = sum(n for _, n in inspection.handled_outside_preview(db, cutoff)) - sum(n for _, n in preview)
    return _render(request, "limpeza.html", user, ate=ate, day=day, preview=preview, only_closed=only_closed,
                   kept=kept, total=sum(n for _, n in preview or []))


@router.post("/limpeza")
def cleanup_apply(ate: str = Form(...), fechadas: str = Form(""), user: User = Depends(current_user),
                  db: Session = Depends(get_db)):
    _require(user, "users")
    try:
        day, cutoff = _cutoff(ate)
    except ValueError:
        return _back("/app/limpeza", erro="Data inválida")
    n = inspection.mark_handled_outside(db, cutoff, user.actor, only_closed=fechadas == "1")
    return _back("/app?aba=conferir", ok=f"{n} devoluções até {day:%d/%m/%Y} marcadas como tratadas fora do sistema")


@router.post("/devolucoes/{return_id}/voltar-fila")
def back_to_queue(return_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "inspect")
    ret = _load(db, return_id)
    inspection.undo_handled_outside(db, ret, user.actor)
    return _back(_detail_url(ret), ok="Devolução voltou para a fila de conferência")


# ---------- pacote não entregue (voltou pela transportadora) ----------

@router.get("/nao-entregue")
def undelivered_page(request: Request, user: User = Depends(current_user)):
    _require(user, "inspect")
    return _render(request, "nao_entregue.html", user)


@router.post("/nao-entregue")
def undelivered_register(
    request: Request,
    codigo: str = Form(...),
    observacoes: str = Form(""),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require(user, "inspect")
    try:
        ret, created = erp.register_undelivered_by_code(db, codigo, user.actor, observacoes.strip() or None)
    except (erp.ErpError, OlistError) as exc:
        db.rollback()
        return _render(request, "nao_entregue.html", user, erro=str(exc), codigo=codigo, observacoes=observacoes)
    if created:
        return _back(_detail_url(ret), ok="Pacote não entregue registrado. Faça a conferência abaixo.")
    return _back(_detail_url(ret), ok="Já existe um registro para esse pedido: abrimos ele para você.")


# ---------- relatórios (gestor) ----------

PERIODS = {
    "mes": "Este mês",
    "mes_anterior": "Mês passado",
    "30d": "Últimos 30 dias",
    "90d": "Últimos 90 dias",
    "ano": "Este ano",
}


def period_range(periodo: str, today: date) -> tuple[date, date]:
    if periodo == "mes_anterior":
        end = today.replace(day=1) - timedelta(days=1)
        return end.replace(day=1), end
    if periodo == "30d":
        return today - timedelta(days=29), today
    if periodo == "90d":
        return today - timedelta(days=89), today
    if periodo == "ano":
        return today.replace(month=1, day=1), today
    return today.replace(day=1), today


class ReportParams:
    """Filtros do painel de Relatórios, vindos da URL (os mesmos no painel, nos dados e no Excel)."""

    def __init__(
        self,
        periodo: str = "",
        inicio: str = "",
        fim: str = "",
        plataforma: str = "",
        loja: str = "",
        motivo: str = "",
        sku: str = "",
        agrupar: str = "day",
    ):
        today = datetime.now(ZoneInfo(get_settings().timezone)).date()
        try:
            start, end = date.fromisoformat(inicio), date.fromisoformat(fim)
        except ValueError:
            start, end = period_range(periodo if periodo in PERIODS else "30d", today)
        if start > end:
            start, end = end, start
        self.start, self.end = start, end
        self.platform = plataforma if plataforma in labels.PLATFORM else None
        try:
            self.store_id = uuid.UUID(loja) if loja else None
        except ValueError:
            self.store_id = None
        self.reason = motivo.strip() or None
        self.sku = sku.strip() or None
        self.group = agrupar if agrupar in ("day", "week", "month") else "day"

    def label(self) -> str:
        return f"{self.start:%d/%m/%Y} a {self.end:%d/%m/%Y}"


@router.get("/relatorios")
def reports_page(request: Request, p: ReportParams = Depends(), user: User = Depends(current_user)):
    """Painel interativo: a página carrega e busca os dados em /app/relatorios/dados a cada filtro."""
    _require(user, "reports")
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    presets = {k: [d.isoformat() for d in period_range(k, today)] for k in PERIODS}
    return _render(request, "relatorios.html", user, p=p, periods=PERIODS, presets=presets, platforms=labels.PLATFORM)


@router.get("/relatorios/dados")
def reports_data(p: ReportParams = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "reports")
    data = dashboard.dashboard_data(db, p.start, p.end, p.platform, p.store_id, p.reason, p.sku, p.group)
    data["queue"] = _counts(db)  # o que espera a loja agora (independe do período)
    f = dashboard.make_filters(p.start, p.end, p.platform, p.store_id, None, None)
    data["repeat"] = buyers.summary(db, f.conds)
    return data


@router.get("/reincidentes")
def repeat_buyers_page(request: Request, p: ReportParams = Depends(), user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    """Compradores com 3 ou mais devoluções no período do painel de Relatórios (só Shopee e Mercado Livre)."""
    _require(user, "reports")
    f = dashboard.make_filters(p.start, p.end, p.platform, p.store_id, None, None)
    return _render(request, "reincidentes.html", user, p=p, buyers_list=buyers.repeat_buyers(db, f.conds),
                   repeat_min=buyers.REPEAT_MIN)


@router.get("/relatorios/produtos")
def reports_products(p: ReportParams = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Só o ranking de produtos, com o período escolhido no próprio bloco."""
    _require(user, "reports")
    f = dashboard.make_filters(p.start, p.end, p.platform, p.store_id, p.reason, None)
    return {"start": p.start.isoformat(), "end": p.end.isoformat(), "products": dashboard.products_data(db, f)}


@router.get("/relatorios/pedidos")
def reports_orders(p: ReportParams = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Pedidos de origem de um produto (painel lateral do ranking), com os filtros do painel menos o SKU."""
    _require(user, "reports")
    if not p.sku:
        return Response(status_code=400)
    f = dashboard.make_filters(p.start, p.end, p.platform, p.store_id, p.reason, None)
    data = dashboard.product_orders(db, f, p.sku)
    for o in data["orders"]:
        o["status_label"], o["status_tone"] = labels.STATUS.get(o["status"] or "unknown", (o["status"], "muted"))
    return data


@router.get("/relatorios/excel")
def reports_excel(p: ReportParams = Depends(), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Os mesmos filtros do painel, em planilha (resumo, lojas, produtos, motivos, meses e a lista)."""
    _require(user, "reports")
    content = excel.build_report_xlsx(
        db, p.start, p.end, p.platform, p.label(),
        store_id=p.store_id, extra=dashboard.extra_conditions(p.reason, p.sku),
        filters_note=", ".join(x for x in (p.reason and f"motivo: {p.reason}", p.sku and f"SKU: {p.sku}") if x),
    )
    name = f"devolucoes_{p.platform or 'todos'}_{p.start:%Y-%m-%d}_a_{p.end:%Y-%m-%d}.xlsx"
    return Response(
        content,
        media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        headers={"Content-Disposition": f'attachment; filename="{name}"'},
    )


# ---------- usuários ----------

@router.get("/usuarios")
def users_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    users = db.scalars(select(User).order_by(User.active.desc(), User.name)).all()
    return _render(request, "usuarios.html", user, users=users, roles=ROLES)


@router.post("/usuarios")
def users_create(
    nome: str = Form(...),
    email: str = Form(...),
    perfil: str = Form(...),
    senha: str = Form(...),
    user: User = Depends(current_user),
    db: Session = Depends(get_db),
):
    _require(user, "users")
    try:
        created = create_user(db, email, nome, perfil, senha)
    except AuthError as exc:
        return _back("/app/usuarios", erro=str(exc))
    return _back("/app/usuarios", ok=f"Usuário {created.name} criado")


# ---------- tela da bancada (fica no enquadramento da câmera) ----------

BENCH_WINDOW = timedelta(minutes=60)


@router.get("/bancada")
def bench(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Mostra em letras grandes o pedido em conferência, para o próprio vídeo provar de qual pacote se trata."""
    now = datetime.now(UTC)
    current = db.scalar(
        select(Return)
        .where(Return.inspection_started_at > now - BENCH_WINDOW, Return.received_at.is_(None))
        .options(selectinload(Return.items))
        .order_by(Return.inspection_started_at.desc())
        .limit(1)
    )
    last = None
    if current is None:  # acabou de conferir: mostra o último por alguns minutos
        last = db.scalar(
            select(Return).where(Return.received_at > now - timedelta(minutes=3))
            .options(selectinload(Return.items)).order_by(Return.received_at.desc()).limit(1)
        )
    return _render(request, "bancada.html", user, current=current, last=last)


# ---------- custos por modelo (gestor) ----------

def _costs_back(ok: str | None = None, erro: str | None = None) -> RedirectResponse:
    return _back("/app/custos", ok=ok, erro=erro)


@router.get("/custos")
def costs_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    models = product_costs.models(db)
    cov = product_costs.coverage(db)
    last = max((m["updated_at"] for m in models if m["updated_at"]), default=None)
    return _render(request, "custos.html", user, models=models, cov=cov, last=last,
                   default_cost=Decimal(str(get_settings().cost_product_default)))


@router.get("/custos/excel")
def costs_excel(user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    cov = product_costs.coverage(db)
    data = excel.build_costs_xlsx(product_costs.models(db), cov["by_model"], cov["missing"])
    return Response(data, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": 'attachment; filename="custos-por-modelo.xlsx"'})


@router.post("/custos/novo")
def costs_create(codigo: str = Form(""), nome: str = Form(""), inicios: str = Form(""), custo: str = Form(""),
                 user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    try:
        code = product_costs.create_model(db, codigo, nome, inicios, custo)
    except product_costs.CostError as exc:
        db.rollback()
        return _costs_back(erro=str(exc))
    return _costs_back(ok=f"Modelo {code} criado")


@router.post("/custos/ligar")
def costs_link(sku: str = Form(...), codigo: str = Form(""), user: User = Depends(current_user),
               db: Session = Depends(get_db)):
    """SKU sem modelo: liga o SKU inteiro a um modelo (vira um começo de SKU desse modelo)."""
    _require(user, "users")
    if not codigo:
        return _costs_back(erro="Escolha o modelo para ligar o SKU")
    try:
        prefix = product_costs.add_prefix(db, codigo, sku)
    except product_costs.CostError as exc:
        db.rollback()
        return _costs_back(erro=str(exc))
    return _costs_back(ok=f"{prefix} ligado ao modelo {codigo}")


@router.post("/custos/{code}/custo")
def costs_set(code: str, custo: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    try:
        value = product_costs.set_model_cost(db, code, custo)
    except product_costs.CostError as exc:
        db.rollback()
        return _costs_back(erro=str(exc))
    return _costs_back(ok=f"{code}: custo da peça agora é {labels.money(value)}")


@router.post("/custos/{code}/inicio")
def costs_add_prefix(code: str, inicio: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    try:
        prefix = product_costs.add_prefix(db, code, inicio)
    except product_costs.CostError as exc:
        db.rollback()
        return _costs_back(erro=str(exc))
    return _costs_back(ok=f"{prefix} agora é do modelo {code}")


@router.post("/custos/inicio/{prefix}/remover")
def costs_remove_prefix(prefix: str, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    try:
        product_costs.remove_prefix(db, prefix)
    except product_costs.CostError as exc:
        db.rollback()
        return _costs_back(erro=str(exc))
    return _costs_back(ok=f"{prefix} removido: esses SKUs voltam a usar o modelo de começo mais curto ou o custo padrão")


# ---------- integrações (chaves de API somente leitura) ----------

def _keys_page(request: Request, user: User, db: Session, **ctx) -> HTMLResponse:
    base = str(request.base_url).rstrip("/")
    public = (get_settings().public_base_url or base).rstrip("/")
    return _render(request, "integracoes.html", user, keys=api_keys.list_keys(db), api_base=f"{public}/integracao/v1",
                   statuses=store_status.all_statuses(db), backup=media_backup.status(db), **ctx)


def _authorize_url(store: Store) -> str:
    # Os mesmos links das rotas /auth/<plataforma>/authorize-url, aqui pelo login do app em vez da chave de API
    from ..routers import auth_mercadolivre, auth_shein, auth_shopee, auth_tiktok

    if store.platform == "tiktok" and store.tiktok_app == 2:
        return auth_tiktok.authorize_url_2()["url"]
    module = {"tiktok": auth_tiktok, "shopee": auth_shopee, "mercado_livre": auth_mercadolivre, "shein": auth_shein}[store.platform]
    return module.authorize_url()["url"]


@router.get("/lojas/{store_id}/autorizar")
def store_authorize(store_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Autorizar de novo: leva ao painel da plataforma (logado na loja) e volta pelo callback de sempre."""
    _require(user, "users")
    store = db.get(Store, store_id)
    if not store:
        raise NotFoundUI()
    try:
        return RedirectResponse(_authorize_url(store), status_code=303)
    except (KeyError, HTTPException, PlatformError) as exc:
        detail = getattr(exc, "detail", None) or str(exc)
        return _back("/app/integracoes", erro=f"Não consegui gerar o link de autorização: {detail}")


@router.post("/lojas/{store_id}/nome")
def store_rename(store_id: uuid.UUID, nome: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Nome da loja nas telas e relatórios. Vazio volta a usar o nome da plataforma."""
    _require(user, "users")
    store = db.get(Store, store_id)
    if not store:
        raise NotFoundUI()
    store.display_name = nome.strip()[:60] or None
    db.commit()
    return RedirectResponse(f"/app/integracoes?{urlencode({'ok': f'Loja agora aparece como {store.label}'})}#lojas", status_code=303)


@router.get("/integracoes")
def keys_page(request: Request, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    return _keys_page(request, user, db)


@router.post("/integracoes")
def keys_create(
    request: Request, nome: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)
):
    _require(user, "users")
    try:
        key, token = api_keys.create_key(db, nome, created_by=user.email)
    except ValueError as exc:
        return _back("/app/integracoes", erro=str(exc))
    # A chave só é mostrada nesta resposta (sem redirecionar, para não ir parar na URL)
    return _keys_page(request, user, db, new_key=token, new_key_name=key.name)


@router.post("/integracoes/{key_id}/revogar")
def keys_revoke(key_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    key = api_keys.revoke_key(db, key_id)
    if key is None:
        return _back("/app/integracoes", erro="Chave não encontrada")
    return _back("/app/integracoes", ok=f"Chave {key.name} revogada: ela para de funcionar na hora")


@router.post("/usuarios/{user_id}/custo")
def users_cost(user_id: uuid.UUID, custo: str = Form(""), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Custo da hora da pessoa (salário + encargos), para o custo por pacote da produtividade."""
    _require(user, "users")
    target = db.get(User, user_id)
    if not target:
        raise NotFoundUI()
    raw = custo.replace("R$", "").strip()
    if "," in raw:  # formato brasileiro: 1.250,50
        raw = raw.replace(".", "").replace(",", ".")
    try:
        target.hourly_cost = Decimal(raw) if raw else None
    except ArithmeticError:
        return _back("/app/usuarios", erro="Custo inválido: use só números, ex.: 18,50")
    db.commit()
    return _back("/app/usuarios", ok=f"Custo/hora de {target.name} salvo")


# ---------- produtividade da bancada (gestor) e ponto do operador ----------

PROD_PERIODS = {"hoje": "Hoje", "ontem": "Ontem", "7d": "7 dias", "30d": "30 dias"}


def _prod_range(periodo: str, data: str) -> tuple[date, date, str]:
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    if data:
        try:
            d = date.fromisoformat(data)
            return d, d, ""
        except ValueError:
            pass
    if periodo == "ontem":
        return today - timedelta(days=1), today - timedelta(days=1), periodo
    if periodo == "7d":
        return today - timedelta(days=6), today, periodo
    if periodo == "30d":
        return today - timedelta(days=29), today, periodo
    return today, today, "hoje"


@router.get("/produtividade")
def productivity_page(request: Request, periodo: str = "hoje", data: str = "", usuario: str = "",
                      user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "reports")
    start, end, periodo = _prod_range(periodo, data)
    d = productivity.collect(db, start, end, usuario or None)
    return _render(request, "produtividade.html", user, d=d, periodo=periodo, periods=PROD_PERIODS,
                   data=data, usuario=usuario)


@router.get("/produtividade/excel")
def productivity_excel(periodo: str = "hoje", data: str = "", user: User = Depends(current_user),
                       db: Session = Depends(get_db)):
    _require(user, "reports")
    start, end, _ = _prod_range(periodo, data)
    content = excel.build_productivity_xlsx(productivity.daily_rows(db, start, end))
    return Response(content, media_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                    headers={"Content-Disposition": f'attachment; filename="produtividade-{start}-{end}.xlsx"'})


@router.post("/ponto")
def punch_clock(acao: str = Form(...), user: User = Depends(current_user), db: Session = Depends(get_db)):
    """Ponto do operador (só para a conta de eficiência): iniciar, pausar, voltar e encerrar o turno."""
    try:
        productivity.punch(db, user, acao)
    except ValueError as exc:
        return _back("/app", erro=str(exc))
    msg = {"shift_start": "Turno iniciado", "pause": "Pausa iniciada", "resume": "De volta ao trabalho",
           "shift_end": "Turno encerrado"}[acao]
    return _back("/app", ok=msg)


@router.post("/usuarios/{user_id}/sair-de-todos")
def users_logout_everywhere(user_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    target = db.get(User, user_id)
    if not target:
        raise NotFoundUI()
    end_all_sessions(db, target)
    return _back("/app/usuarios", ok=f"{target.name} saiu de todos os aparelhos")


@router.post("/usuarios/{user_id}/ativo")
def users_toggle(user_id: uuid.UUID, user: User = Depends(current_user), db: Session = Depends(get_db)):
    _require(user, "users")
    target = db.get(User, user_id)
    if not target or target.id == user.id:
        return _back("/app/usuarios", erro="Você não pode desativar o próprio usuário")
    target.active = not target.active
    db.commit()
    return _back("/app/usuarios", ok=f"{target.name} {'reativado' if target.active else 'desativado'}")
