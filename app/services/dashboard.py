"""Dados do painel interativo de Relatórios (filtros cruzados + comparação com o período anterior).

Reaproveita as consultas de app/routers/reports.py, acrescentando aos filtros padrão (data, marketplace,
loja) os filtros de clique do painel: motivo e SKU.
"""

import uuid
from datetime import date, timedelta
from typing import Any

from sqlalchemy import exists, func, select
from sqlalchemy.orm import Session, aliased

from ..config import get_settings
from ..models import Return, ReturnItem, Store
from ..routers import reports

NO_REASON = "Sem motivo"
NO_SKU = "SEM SKU"
INSPECTED = ("resale", "defect", "mismatch")


def extra_conditions(reason: str | None = None, sku: str | None = None) -> list:
    conds = []
    if reason:
        conds.append(func.coalesce(Return.reason_text, Return.reason_code, NO_REASON) == reason)
    if sku:
        # Apelido próprio: as consultas por produto já juntam return_items, e sem ele o EXISTS perde a tabela
        ri = aliased(ReturnItem)
        item = ri.seller_sku.is_(None) if sku == NO_SKU else ri.seller_sku == sku
        conds.append(exists().where(ri.return_id == Return.id, item))
    return conds


def make_filters(
    start: date, end: date, platform: str | None, store_id: uuid.UUID | None, reason: str | None, sku: str | None
) -> reports.Filters:
    f = reports.Filters(date_from=start, date_to=end, platform=platform, store_id=store_id)
    f.conds += extra_conditions(reason, sku)
    return f


def _metrics(db: Session, f: reports.Filters) -> dict[str, Any]:
    s = reports.summary(f=f, db=db)
    totals = reports.costs(f=f, group_by="reason", db=db)["totals"]
    cond = {c["condition"]: c for c in reports.by_condition(f=f, db=db)}
    inspected = sum(cond.get(k, {}).get("received", 0) for k in INSPECTED)
    defect = cond.get("defect", {}).get("received", 0)
    return {
        "total": s["total"],
        "cost": totals["total"],
        "refunded": s["refunded_amount"],
        "avg_days": round(s["avg_resolution_hours"] / 24, 1) if s["avg_resolution_hours"] is not None else None,
        "inspected": inspected,
        "defect_rate": round(defect / inspected, 4) if inspected else None,
        "mismatch": cond.get("mismatch", {}).get("received", 0),
    }


def _cost_by_period(db: Session, f: reports.Filters, group: str) -> dict[str, float]:
    """Custo para a empresa por período (mesmo fuso e agrupamento da evolução)."""
    bucket = func.date_trunc(group, func.timezone(get_settings().timezone, Return.requested_at)).label("bucket")
    try:
        rows = db.execute(
            select(bucket, func.sum(Return.cost_total)).where(*f.conds, Return.requested_at.is_not(None)).group_by(bucket)
        ).all()
        return {b.date().isoformat(): float(c or 0) for b, c in rows}
    except (TypeError, ValueError, AttributeError):  # banco de teste sem esta consulta
        return {}


def _series(db: Session, f: reports.Filters, start: date, end: date, group: str) -> list[dict[str, Any]]:
    rows = {r["period"]: r for r in reports.timeseries(f=f, granularity=group, db=db)}
    cost = _cost_by_period(db, f, group)
    if group != "day":
        return [{"period": k, "total": v["total"], "amount": v["amount"], "cost": cost.get(k, 0)} for k, v in sorted(rows.items())]
    out, d = [], start
    while d <= end:  # dias sem devolução aparecem como zero
        k = d.isoformat()
        r = rows.get(k)
        out.append({"period": k, "total": r["total"] if r else 0, "amount": r["amount"] if r else 0, "cost": cost.get(k, 0)})
        d += timedelta(days=1)
    return out


REASON_SLOTS = 3  # motivos com cor própria na barra de cada produto; o resto vira "Outros"


def _sku_cond(sku: str | None):
    return ReturnItem.seller_sku.is_(None) if sku in (None, NO_SKU) else ReturnItem.seller_sku == sku


def product_extras(db: Session, f: reports.Filters, skus: list[str | None]) -> dict[str | None, dict[str, Any]]:
    """Foto e motivos de cada produto do ranking (a foto vem do marketplace, na devolução mais recente)."""
    if not skus:
        return {}
    named = [s for s in skus if s is not None]
    sku_in = ReturnItem.seller_sku.in_(named) if None not in skus else (
        ReturnItem.seller_sku.in_(named) | ReturnItem.seller_sku.is_(None))
    out: dict[str | None, dict[str, Any]] = {s: {"image": None, "reasons": []} for s in skus}
    for sku, image in db.execute(
        select(ReturnItem.seller_sku, func.max(ReturnItem.image_url))
        .join(Return, Return.id == ReturnItem.return_id)
        .where(*f.conds, sku_in, ReturnItem.image_url.is_not(None))
        .group_by(ReturnItem.seller_sku)
    ):
        if sku in out:
            out[sku]["image"] = image
    reason = func.coalesce(Return.reason_text, Return.reason_code, NO_REASON)
    for sku, why, n in db.execute(
        select(ReturnItem.seller_sku, reason, func.count(func.distinct(Return.id)))
        .join(Return, Return.id == ReturnItem.return_id)
        .where(*f.conds, sku_in)
        .group_by(ReturnItem.seller_sku, reason)
        .order_by(func.count(func.distinct(Return.id)).desc())
    ):
        if sku in out:
            out[sku]["reasons"].append({"reason": why, "total": n})
    return out


def product_orders(db: Session, f: reports.Filters, sku: str, limit: int = 300) -> dict[str, Any]:
    """Pedidos de origem de um produto no período (painel lateral do ranking)."""
    rows = db.execute(
        select(Return, ReturnItem.sku_name, ReturnItem.image_url, ReturnItem.product_name)
        .join(ReturnItem, ReturnItem.return_id == Return.id)
        .where(*f.conds, _sku_cond(sku))
        .order_by(Return.requested_at.desc())
        .limit(limit)
    ).all()
    seen: set = set()
    orders = []
    for ret, variant, image, name in rows:
        if ret.id in seen:
            continue
        seen.add(ret.id)
        orders.append({
            "id": str(ret.id), "platform": ret.platform, "order": ret.external_order_id or ret.external_return_id,
            "reason": ret.reason_text or ret.reason_code or NO_REASON, "variant": variant,
            "requested_at": ret.requested_at.isoformat() if ret.requested_at else None,
            "status": ret.status, "condition": ret.item_condition, "received": ret.received_at is not None,
            "image": image, "name": name,
        })
    return {"sku": sku, "orders": orders, "limited": len(rows) >= limit}


def products_data(db: Session, f: reports.Filters, top: list[dict[str, Any]] | None = None,
                  sku_cost: dict | None = None) -> list[dict[str, Any]]:
    """Ranking de produtos com foto, motivos e custo."""
    if top is None:
        top = reports.by_product(f=f, limit=50, db=db)
    if sku_cost is None:
        sku_cost = {g["key"]: g["total"] for g in reports.costs(f=f, group_by="sku", db=db)["groups"]}
    extras = product_extras(db, f, [p["seller_sku"] for p in top])
    return [
        {
            "sku": p["seller_sku"] or NO_SKU, "name": p["product_name"], "total": p["returns"], "units": p["units"],
            "defects": p["defects"], "defect_rate": p["defect_rate"], "mismatches": p["mismatches"],
            "cost": sku_cost.get(p["seller_sku"]),
            "image": extras.get(p["seller_sku"], {}).get("image"),
            "reasons": extras.get(p["seller_sku"], {}).get("reasons", []),
        }
        for p in top
    ]


def dashboard_data(
    db: Session,
    start: date,
    end: date,
    platform: str | None = None,
    store_id: uuid.UUID | None = None,
    reason: str | None = None,
    sku: str | None = None,
    group: str = "day",
) -> dict[str, Any]:
    f = make_filters(start, end, platform, store_id, reason, sku)
    # Período anterior com o mesmo número de dias, logo antes do escolhido
    days = (end - start).days + 1
    prev_end = start - timedelta(days=1)
    prev_start = prev_end - timedelta(days=days - 1)
    prev = make_filters(prev_start, prev_end, platform, store_id, reason, sku)

    by_platform = db.execute(
        select(Return.platform, func.count()).where(*f.conds).group_by(Return.platform).order_by(func.count().desc())
    ).all()
    reasons = sorted(reports.costs(f=f, group_by="reason", db=db)["groups"], key=lambda g: g["returns"], reverse=True)
    sku_cost = {g["key"]: g["total"] for g in reports.costs(f=f, group_by="sku", db=db)["groups"]}
    cond = {c["condition"]: c for c in reports.by_condition(f=f, db=db)}
    physical = sum(c["total"] for c in cond.values())
    top = reports.by_product(f=f, limit=50, db=db)
    inspected = {k: cond.get(k, {}).get("received", 0) for k in INSPECTED}

    return {
        "filters": {
            "start": start.isoformat(), "end": end.isoformat(), "platform": platform, "store_id": str(store_id) if store_id else None,
            "reason": reason, "sku": sku, "group": group,
        },
        "previous": {"start": prev_start.isoformat(), "end": prev_end.isoformat()},
        "kpis": {"current": _metrics(db, f), "previous": _metrics(db, prev)},
        "series": _series(db, f, start, end, group),
        "platforms": [{"platform": p, "total": n} for p, n in by_platform],
        "reasons": [{"reason": g["key"], "total": g["returns"], "cost": g["total"]} for g in reasons[:12]],
        "conditions": {**inspected, "not_inspected": max(0, physical - sum(inspected.values()))},
        "products": products_data(db, f, top, sku_cost),
        "stores": [
            {"id": str(s.id), "name": s.label, "platform": s.platform}
            for s in db.scalars(select(Store).order_by(Store.platform, Store.name)).all()
        ],
    }
