# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Relatórios e métricas de devoluções.

Todos aceitam os mesmos filtros: date_from/date_to (data da solicitação), platform e store_id.
"""

import uuid
from datetime import UTC, date, datetime

from fastapi import APIRouter, Depends, Query
from sqlalchemy import case, func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..enums import ACTIONABLE_STATUSES, ItemCondition, Platform, ReturnKind, ReturnStatus
from ..models import RejectionReason, Return, ReturnItem, Store
from ..security import require_api_key
from .filters import date_range

router = APIRouter(prefix="/reports", tags=["Relatórios"], dependencies=[Depends(require_api_key)])


class Filters:
    def __init__(
        self,
        date_from: date | None = None,
        date_to: date | None = None,
        platform: Platform | None = None,
        store_id: uuid.UUID | None = None,
        kind: ReturnKind | None = None,
    ):
        self.conds = date_range(Return.requested_at, date_from, date_to)
        if platform:
            self.conds.append(Return.platform == platform)
        if store_id:
            self.conds.append(Return.store_id == store_id)
        if kind:
            self.conds.append(Return.kind == kind)


def _n(value) -> float:
    return float(value) if value is not None else 0.0


def _count_if(cond):
    return func.count(case((cond, 1)))


@router.get("/summary")
def summary(f: Filters = Depends(), db: Session = Depends(get_db)):
    """Visão geral: volumes, taxa de aprovação, valores reembolsados e tempo de resolução."""
    now = datetime.now(UTC)
    row = db.execute(
        select(
            func.count().label("total"),
            _count_if(Return.status.in_(ACTIONABLE_STATUSES)).label("needs_action"),
            _count_if(Return.status.in_(ACTIONABLE_STATUSES) & (Return.action_deadline < now)).label("overdue"),
            _count_if(Return.status == ReturnStatus.COMPLETED).label("completed"),
            _count_if(Return.status == ReturnStatus.REJECTED).label("rejected"),
            _count_if(Return.status == ReturnStatus.CANCELLED).label("cancelled"),
            func.sum(case((Return.status == ReturnStatus.COMPLETED, Return.refund_amount))).label("refunded"),
            func.sum(Return.refund_amount).label("requested_amount"),
            func.avg(func.extract("epoch", Return.resolved_at - Return.requested_at) / 3600).label("avg_hours"),
        ).where(*f.conds)
    ).one()

    by_status = db.execute(
        select(Return.status, func.count()).where(*f.conds).group_by(Return.status)
    ).all()
    by_type = db.execute(
        select(Return.return_type, func.count()).where(*f.conds).group_by(Return.return_type)
    ).all()
    by_kind = db.execute(select(Return.kind, func.count()).where(*f.conds).group_by(Return.kind)).all()

    decided = row.completed + row.rejected
    return {
        "total": row.total,
        "needs_action": row.needs_action,
        "overdue": row.overdue,
        "completed": row.completed,
        "rejected": row.rejected,
        "cancelled": row.cancelled,
        "approval_rate": round(row.completed / decided, 4) if decided else None,
        "refunded_amount": _n(row.refunded),
        "requested_amount": _n(row.requested_amount),
        "avg_resolution_hours": round(_n(row.avg_hours), 1) if row.avg_hours is not None else None,
        "by_status": {s: c for s, c in by_status},
        "by_type": {t: c for t, c in by_type},
        "by_kind": {k: c for k, c in by_kind},
    }


@router.get("/by-reason")
def by_reason(f: Filters = Depends(), db: Session = Depends(get_db)):
    """Motivos alegados pelos compradores."""
    rows = db.execute(
        select(
            func.coalesce(Return.reason_text, Return.reason_code, "Sem motivo").label("reason"),
            func.count().label("count"),
            func.sum(Return.refund_amount).label("amount"),
        )
        .where(*f.conds)
        .group_by("reason")
        .order_by(func.count().desc())
    ).all()
    return [{"reason": r.reason, "count": r.count, "amount": _n(r.amount)} for r in rows]


@router.get("/by-product")
def by_product(
    f: Filters = Depends(), limit: int = Query(20, ge=1, le=200), db: Session = Depends(get_db)
):
    """Produtos/SKUs mais devolvidos."""
    rows = db.execute(
        select(
            ReturnItem.seller_sku,
            func.max(ReturnItem.product_name).label("product_name"),
            func.count(func.distinct(Return.id)).label("returns"),
            func.sum(ReturnItem.quantity).label("units"),
            func.sum(ReturnItem.refund_amount).label("amount"),
            func.count(func.distinct(case((Return.item_condition == ItemCondition.DEFECT, Return.id)))).label(
                "defects"
            ),
            func.count(func.distinct(case((Return.item_condition == ItemCondition.MISMATCH, Return.id)))).label(
                "mismatches"
            ),
        )
        .join(Return, Return.id == ReturnItem.return_id)
        .where(*f.conds)
        .group_by(ReturnItem.seller_sku)
        .order_by(func.count(func.distinct(Return.id)).desc())
        .limit(limit)
    ).all()
    return [
        {
            "seller_sku": r.seller_sku,
            "product_name": r.product_name,
            "returns": r.returns,
            "units": int(r.units or 0),
            "amount": _n(r.amount),
            "defects": r.defects,
            "defect_rate": round(r.defects / r.returns, 4) if r.returns else None,
            # Veio outro produto no pacote (possível golpe)
            "mismatches": r.mismatches,
        }
        for r in rows
    ]


@router.get("/costs")
def costs(
    f: Filters = Depends(),
    group_by: str = Query("reason", pattern="^(reason|liability|month|store|sku|condition)$"),
    db: Session = Depends(get_db),
):
    """Custo das devoluções para a empresa (frete pago pela loja + perda de peça + operacional).

    `refunded_amount` é a receita devolvida (reembolsos concluídos), reportada à parte do custo.
    """
    conds = [*f.conds, Return.status != ReturnStatus.CANCELLED]
    tz = get_settings().timezone
    keys = {
        "reason": func.coalesce(Return.reason_text, Return.reason_code, "Sem motivo"),
        "liability": func.coalesce(Return.liability, "none"),
        "month": func.to_char(func.timezone(tz, Return.requested_at), "YYYY-MM"),
        "store": func.coalesce(func.nullif(func.trim(Store.display_name), ""), Store.name),
        "condition": func.coalesce(Return.item_condition, "unclassified"),
    }

    totals = select(
        func.count().label("returns"),
        func.sum(Return.cost_return_shipping).label("shipping"),
        func.sum(Return.cost_product_loss).label("product_loss"),
        func.sum(Return.cost_operational).label("operational"),
        func.sum(Return.cost_lost_sale).label("lost_sale"),
        func.count(Return.cost_settled_at).label("settled"),
        func.sum(Return.cost_total).label("total"),
        func.sum(case((Return.status == ReturnStatus.COMPLETED, Return.refund_amount))).label("refunded"),
    )

    if group_by == "sku":
        # Rateia o custo da devolução pelos itens (normalmente 1 item por devolução)
        n_items = (
            select(func.count()).where(ReturnItem.return_id == Return.id).correlate(Return).scalar_subquery()
        )
        share = Return.cost_total / func.nullif(n_items, 0)
        stmt = (
            select(
                ReturnItem.seller_sku.label("key"),
                func.count(func.distinct(Return.id)).label("returns"),
                func.sum(share).label("total"),
                func.sum(case((Return.liability == "seller", 1), else_=0)).label("seller_fault"),
            )
            .join(Return, Return.id == ReturnItem.return_id)
            .where(*conds)
            .group_by(ReturnItem.seller_sku)
            .order_by(func.sum(share).desc())
            .limit(50)
        )
        rows = db.execute(stmt).all()
        groups = [
            {"key": r.key, "returns": r.returns, "total": _n(r.total), "seller_fault_returns": r.seller_fault}
            for r in rows
        ]
    else:
        key = keys[group_by].label("key")
        stmt = totals.add_columns(key).where(*conds).group_by(key).order_by(func.sum(Return.cost_total).desc())
        if group_by == "store":
            stmt = stmt.join(Store, Store.id == Return.store_id)
        groups = [
            {
                "key": r.key,
                "returns": r.returns,
                "shipping": _n(r.shipping),
                "product_loss": _n(r.product_loss),
                "operational": _n(r.operational),
                "lost_sale": _n(r.lost_sale),
                "total": _n(r.total),
                "avg_per_return": round(_n(r.total) / r.returns, 2) if r.returns else None,
                "refunded_amount": _n(r.refunded),
            }
            for r in db.execute(stmt).all()
        ]

    t = db.execute(totals.where(*conds)).one()
    return {
        "totals": {
            "returns": t.returns,
            "shipping": _n(t.shipping),
            "product_loss": _n(t.product_loss),
            "operational": _n(t.operational),
            "lost_sale": _n(t.lost_sale),
            "total": _n(t.total),
            "settled_returns": t.settled,  # quantas com custo confirmado pelo extrato (TikTok)
            "refunded_amount": _n(t.refunded),
        },
        "group_by": group_by,
        "groups": groups,
    }


@router.get("/by-condition")
def by_condition(f: Filters = Depends(), db: Session = Depends(get_db)):
    """Revenda x defeito, com situação da NF de devolução e do estoque no Olist."""
    conds = [*f.conds, Return.return_type != "refund_only"]
    rows = db.execute(
        select(
            func.coalesce(Return.item_condition, "unclassified").label("condition"),
            func.count().label("total"),
            _count_if(Return.received_at.is_not(None)).label("received"),
            _count_if(Return.erp_return_invoice_status == "authorized").label("invoice_authorized"),
            _count_if(Return.erp_return_invoice_status == "draft").label("invoice_draft"),
            _count_if(Return.received_at.is_not(None) & Return.erp_return_invoice_id.is_(None)).label(
                "invoice_missing"
            ),
            _count_if(Return.erp_stock_status == "posted").label("stock_posted"),
            func.sum(Return.refund_amount).label("amount"),
        )
        .where(*conds)
        .group_by("condition")
    ).all()
    return [
        {
            "condition": r.condition,
            "total": r.total,
            "received": r.received,
            "invoice_authorized": r.invoice_authorized,
            "invoice_draft": r.invoice_draft,
            "invoice_missing": r.invoice_missing,
            "stock_posted": r.stock_posted,
            "amount": _n(r.amount),
        }
        for r in rows
    ]


@router.get("/by-store")
def by_store(f: Filters = Depends(), db: Session = Depends(get_db)):
    rows = db.execute(
        select(
            Store.id,
            func.coalesce(func.nullif(func.trim(Store.display_name), ""), Store.name).label("name"),
            Store.platform,
            func.count(Return.id).label("total"),
            _count_if(Return.status.in_(ACTIONABLE_STATUSES)).label("needs_action"),
            _count_if(Return.status == ReturnStatus.COMPLETED).label("completed"),
            _count_if(Return.status == ReturnStatus.REJECTED).label("rejected"),
            func.sum(case((Return.status == ReturnStatus.COMPLETED, Return.refund_amount))).label("refunded"),
        )
        .join(Return, Return.store_id == Store.id)
        .where(*f.conds)
        .group_by(Store.id, Store.name, Store.display_name, Store.platform)
        .order_by(func.count(Return.id).desc())
    ).all()
    return [
        {
            "store_id": str(r.id),
            "store_name": r.name,
            "platform": r.platform,
            "total": r.total,
            "needs_action": r.needs_action,
            "completed": r.completed,
            "rejected": r.rejected,
            "refunded_amount": _n(r.refunded),
        }
        for r in rows
    ]


@router.get("/rejections")
def rejections(f: Filters = Depends(), db: Session = Depends(get_db)):
    """Recusas feitas pela API, agrupadas pelo motivo interno e pelo motivo enviado à plataforma."""
    conds = [*f.conds, Return.decision == "rejected"]
    internal = db.execute(
        select(
            func.coalesce(RejectionReason.label, "Não informado").label("reason"),
            func.count(Return.id).label("count"),
        )
        .select_from(Return)
        .outerjoin(RejectionReason, RejectionReason.code == Return.internal_reject_reason_code)
        .where(*conds)
        .group_by("reason")
        .order_by(func.count(Return.id).desc())
    ).all()
    platform = db.execute(
        select(
            func.coalesce(Return.platform_reject_reason_text, Return.platform_reject_reason_code).label("reason"),
            func.count().label("count"),
        )
        .where(*conds)
        .group_by("reason")
        .order_by(func.count().desc())
    ).all()
    by_actor = db.execute(
        select(func.coalesce(Return.decided_by, "Não informado").label("actor"), func.count().label("count"))
        .where(*conds)
        .group_by("actor")
    ).all()
    return {
        "by_internal_reason": [{"reason": r.reason, "count": r.count} for r in internal],
        "by_platform_reason": [{"reason": r.reason, "count": r.count} for r in platform],
        "by_actor": [{"actor": r.actor, "count": r.count} for r in by_actor],
    }


@router.get("/timeseries")
def timeseries(
    f: Filters = Depends(),
    granularity: str = Query("day", pattern="^(day|week|month)$"),
    db: Session = Depends(get_db),
):
    """Evolução no tempo (no fuso configurado em TIMEZONE)."""
    bucket = func.date_trunc(granularity, func.timezone(get_settings().timezone, Return.requested_at)).label("bucket")
    rows = db.execute(
        select(
            bucket,
            func.count().label("total"),
            _count_if(Return.status == ReturnStatus.COMPLETED).label("completed"),
            _count_if(Return.status == ReturnStatus.REJECTED).label("rejected"),
            func.sum(Return.refund_amount).label("amount"),
        )
        .where(*f.conds, Return.requested_at.is_not(None))
        .group_by(bucket)
        .order_by(bucket)
    ).all()
    return [
        {
            "period": r.bucket.date().isoformat(),
            "total": r.total,
            "completed": r.completed,
            "rejected": r.rejected,
            "amount": _n(r.amount),
        }
        for r in rows
    ]
