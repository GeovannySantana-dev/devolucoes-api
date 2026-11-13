# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import exists, func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..db import get_db
from ..enums import ACTIONABLE_STATUSES, EventType, ItemCondition, Platform, ReturnKind, ReturnStatus
from ..models import RejectionReason, Return, ReturnEvent, ReturnItem
from ..platforms.base import PlatformError, PlatformNotSupported
from ..platforms.registry import get_adapter
from ..schemas import (
    ApproveIn,
    NoteIn,
    ReasonOut,
    RejectIn,
    RejectionReasonIn,
    RejectReasonsOut,
    ReturnDetailOut,
    ReturnPage,
)
from ..security import require_api_key
from ..services import actions
from ..services.sync import upsert_return
from .filters import date_range

router = APIRouter(tags=["Devoluções"], dependencies=[Depends(require_api_key)])


def _load(db: Session, return_id: uuid.UUID) -> Return:
    ret = db.scalar(
        select(Return)
        .where(Return.id == return_id)
        .options(selectinload(Return.items), selectinload(Return.events), selectinload(Return.store))
    )
    if not ret:
        raise HTTPException(404, "Devolução não encontrada")
    return ret


def _platform_call(fn):
    try:
        return fn()
    except PlatformNotSupported as exc:
        raise HTTPException(501, str(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.get("/returns", response_model=ReturnPage)
def list_returns(
    platform: Platform | None = None,
    store_id: uuid.UUID | None = None,
    kind: ReturnKind | None = Query(None, description="customer_return ou undelivered (não entregue)"),
    status: list[ReturnStatus] | None = Query(None),
    needs_action: bool | None = Query(None, description="Somente devoluções aguardando decisão do vendedor"),
    overdue: bool | None = Query(None, description="Somente com prazo de ação vencido"),
    decision: str | None = Query(None, pattern="^(approved|rejected)$"),
    condition: ItemCondition | None = Query(None, description="resale (volta ao estoque) ou defect"),
    erp_invoice_status: str | None = Query(
        None, pattern="^(none|draft|authorized|cancelled)$", description="none = NF de devolução ainda não criada"
    ),
    has_erp_error: bool | None = None,
    q: str | None = Query(None, description="Busca por nº da devolução, pedido, código de rastreio ou SKU"),
    date_from: date | None = None,
    date_to: date | None = None,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    sort: str = Query("-requested_at", pattern="^-?(requested_at|action_deadline|refund_amount|platform_updated_at)$"),
    db: Session = Depends(get_db),
):
    conds = []
    if platform:
        conds.append(Return.platform == platform)
    if store_id:
        conds.append(Return.store_id == store_id)
    if kind:
        conds.append(Return.kind == kind)
    if status:
        conds.append(Return.status.in_(status))
    if needs_action is not None:
        cond = Return.status.in_(ACTIONABLE_STATUSES)
        conds.append(cond if needs_action else ~cond)
    if overdue:
        conds.append(Return.status.in_(ACTIONABLE_STATUSES))
        conds.append(Return.action_deadline < datetime.now(UTC))
    if decision:
        conds.append(Return.decision == decision)
    if condition:
        conds.append(Return.item_condition == condition)
    if erp_invoice_status == "none":
        conds.append(Return.erp_return_invoice_id.is_(None))
    elif erp_invoice_status:
        conds.append(Return.erp_return_invoice_status == erp_invoice_status)
    if has_erp_error is not None:
        conds.append(Return.erp_last_error.is_not(None) if has_erp_error else Return.erp_last_error.is_(None))
    if q:
        like = f"%{q}%"
        sku_match = exists().where(
            ReturnItem.return_id == Return.id,
            or_(ReturnItem.seller_sku.ilike(like), ReturnItem.product_name.ilike(like)),
        )
        conds.append(
            or_(
                Return.external_return_id == q,
                Return.external_order_id == q,
                Return.tracking_number == q.upper(),
                sku_match,
            )
        )
    conds += date_range(Return.requested_at, date_from, date_to)

    total = db.scalar(select(func.count()).select_from(Return).where(*conds))

    col = getattr(Return, sort.lstrip("-"))
    order = col.desc().nulls_last() if sort.startswith("-") else col.asc().nulls_last()
    rows = db.scalars(
        select(Return)
        .where(*conds)
        .options(selectinload(Return.items))
        .order_by(order)
        .offset((page - 1) * page_size)
        .limit(page_size)
    ).all()
    return {"total": total, "page": page, "page_size": page_size, "items": rows}


@router.get("/returns/{return_id}", response_model=ReturnDetailOut)
def get_return(return_id: uuid.UUID, db: Session = Depends(get_db)):
    return _load(db, return_id)


@router.post("/returns/{return_id}/refresh", response_model=ReturnDetailOut)
def refresh_return(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Relê a devolução direto na plataforma."""
    ret = _load(db, return_id)
    nr = _platform_call(lambda: get_adapter(db, ret.store).fetch_return(ret.store, ret.external_return_id))
    if nr:
        upsert_return(db, ret.store, nr, source="api")
        db.commit()
    return _load(db, return_id)


@router.get("/returns/{return_id}/reject-reasons", response_model=RejectReasonsOut)
def reject_reasons(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Motivos aceitos pela plataforma para esta devolução + catálogo interno."""
    ret = _load(db, return_id)
    platform = _platform_call(lambda: get_adapter(db, ret.store).reject_reasons(ret.store, ret))
    internal = db.scalars(select(RejectionReason).where(RejectionReason.active).order_by(RejectionReason.label))
    return {
        "platform": [ReasonOut(code=r.code, label=r.label) for r in platform],
        "internal": [ReasonOut(code=r.code, label=r.label) for r in internal],
    }


@router.post("/returns/{return_id}/approve", response_model=ReturnDetailOut)
def approve_return(return_id: uuid.UUID, body: ApproveIn, db: Session = Depends(get_db)):
    ret = _load(db, return_id)
    try:
        actions.approve(db, ret, body.actor, body.note, body.decision)
    except actions.ActionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PlatformNotSupported as exc:
        raise HTTPException(501, str(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _load(db, return_id)


@router.post("/returns/{return_id}/reject", response_model=ReturnDetailOut)
def reject_return(return_id: uuid.UUID, body: RejectIn, db: Session = Depends(get_db)):
    ret = _load(db, return_id)
    try:
        actions.reject(
            db, ret, body.actor, body.platform_reason_code, body.internal_reason_code, body.comment, body.decision
        )
    except actions.ActionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except PlatformNotSupported as exc:
        raise HTTPException(501, str(exc)) from exc
    except PlatformError as exc:
        raise HTTPException(502, str(exc)) from exc
    return _load(db, return_id)


@router.post("/returns/{return_id}/notes", response_model=ReturnDetailOut)
def add_note(return_id: uuid.UUID, body: NoteIn, db: Session = Depends(get_db)):
    """Anotação interna (não vai para a plataforma)."""
    ret = _load(db, return_id)
    ret.internal_notes = f"{ret.internal_notes}\n{body.note}" if ret.internal_notes else body.note
    ret.events.append(ReturnEvent(event_type=EventType.NOTE, source="api", actor=body.actor, message=body.note))
    db.commit()
    return _load(db, return_id)


# ---------- catálogo interno de motivos de recusa ----------

@router.get("/rejection-reasons", response_model=list[RejectionReasonIn], tags=["Motivos de recusa"])
def list_rejection_reasons(db: Session = Depends(get_db)):
    return db.scalars(select(RejectionReason).order_by(RejectionReason.label)).all()


@router.put("/rejection-reasons/{code}", response_model=RejectionReasonIn, tags=["Motivos de recusa"])
def upsert_rejection_reason(code: str, body: RejectionReasonIn, db: Session = Depends(get_db)):
    if code != body.code:
        raise HTTPException(422, "code da URL difere do body")
    reason = db.get(RejectionReason, code) or RejectionReason(code=code)
    reason.label = body.label
    reason.active = body.active
    db.add(reason)
    db.commit()
    return reason
