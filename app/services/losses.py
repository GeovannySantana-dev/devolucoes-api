"""Perdas do mês: peças conferidas como defeito, que são descartadas.

Viram perda no IRPJ/CSLL com a NF de baixa de estoque por perda (CFOP 5.927), feita por mês. O contador pede
o motivo e as fotos registrados para cada peça: esta lista é a base e a prova dessa nota.
"""

from collections import Counter
from datetime import UTC, date, datetime
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..enums import LOSS_CONDITIONS, ItemCondition
from ..models import Return, ReturnMedia
from ..ui.labels import DEFECT_REASON as DEFECT_REASON_LABELS


def month_bounds(month: str | None, today: date) -> tuple[date, datetime, datetime]:
    """"2026-10" -> primeiro dia do mês e o intervalo [início, fim) em UTC, no fuso da empresa."""
    try:
        first = datetime.strptime(month or "", "%Y-%m").date().replace(day=1)
    except ValueError:
        first = today.replace(day=1)
    nxt = first.replace(year=first.year + (first.month == 12), month=first.month % 12 + 1)
    tz = ZoneInfo(get_settings().timezone)
    start = datetime(first.year, first.month, 1, tzinfo=tz).astimezone(UTC)
    end = datetime(nxt.year, nxt.month, 1, tzinfo=tz).astimezone(UTC)
    return first, start, end


def shift_month(first: date, delta: int) -> str:
    n = first.year * 12 + first.month - 1 + delta
    return f"{n // 12}-{n % 12 + 1:02d}"


def collect(db: Session, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Uma linha por devolução com defeito conferida no período, da mais recente para a mais antiga."""
    photos = (
        select(ReturnMedia.return_id, func.count().label("n"))
        .where(ReturnMedia.kind == "photo")
        .group_by(ReturnMedia.return_id)
        .subquery()
    )
    q = (
        select(Return, func.coalesce(photos.c.n, 0))
        .outerjoin(photos, photos.c.return_id == Return.id)
        .where(Return.item_condition == ItemCondition.DEFECT, Return.received_at >= start, Return.received_at < end)
        .options(selectinload(Return.items))
        .order_by(Return.received_at.desc())
    )
    rows = []
    for ret, n_photos in db.execute(q).all():
        items = [
            {"sku": i.seller_sku or "", "name": i.product_name or "", "variant": i.sku_name or "",
             "quantity": i.quantity or 1, "amount": i.refund_amount}
            for i in ret.items
        ] or [{"sku": "", "name": "", "variant": "", "quantity": 1, "amount": None}]
        rows.append({
            "ret": ret,
            "items": items,
            "pieces": sum(i["quantity"] for i in items),
            "reason": ret.defect_reason,
            "notes": ret.inspection_notes,
            "cost": ret.cost_product_loss or Decimal(0),
            "photos": n_photos,
        })
    return rows


def summarize(rows: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = Counter(r["reason"] or "" for r in rows)
    top = next((k for k, _ in reasons.most_common() if k), None)
    return {
        "returns": len(rows),
        "pieces": sum(r["pieces"] for r in rows),
        "cost": sum((r["cost"] for r in rows), Decimal(0)),
        "with_photo": sum(1 for r in rows if r["photos"]),
        "without_reason": reasons.get("", 0),
        "top_reason": top,
        "by_reason": reasons.most_common(),
    }


def by_sku(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Peças somadas por SKU: é o que vai nos itens da NF 5.927."""
    acc: dict[str, dict[str, Any]] = {}
    for r in rows:
        for i in r["items"]:
            key = i["sku"] or "(sem SKU)"
            a = acc.setdefault(key, {"sku": key, "name": i["name"], "variant": i["variant"], "quantity": 0,
                                     "orders": []})
            a["quantity"] += i["quantity"]
            a["orders"].append(r["ret"].external_order_id or "")
    return sorted(acc.values(), key=lambda a: (-a["quantity"], a["sku"]))


def by_model(db: Session, start: datetime, end: datetime) -> list[dict[str, Any]]:
    """Perda por modelo (defeito e item diferente) em R$, com a taxa sobre as peças conferidas do modelo.

    Usa o custo atual de cada modelo (tela Custos por modelo). Peça sem modelo entra como "Sem modelo".
    """
    from .product_costs import model_for, unit_cost

    rets = db.scalars(
        select(Return)
        .where(Return.received_at >= start, Return.received_at < end)
        .options(selectinload(Return.items))
    ).all()
    acc: dict[str, dict[str, Any]] = {}
    for ret in rets:
        loss = ret.item_condition in LOSS_CONDITIONS and ret.condition_source == "manual"
        reason = (DEFECT_REASON_LABELS.get(ret.defect_reason or "", "sem motivo")
                  if ret.item_condition == ItemCondition.DEFECT else "item diferente")
        for item in ret.items or []:
            found = model_for(item.seller_sku)
            code, name = (found[0], found[1]) if found else ("—", "Sem modelo")
            m = acc.setdefault(code, {"code": code, "name": name, "inspected": 0, "lost": 0, "cost": Decimal(0),
                                      "reasons": Counter()})
            qty = item.quantity or 1
            m["inspected"] += qty
            if loss:
                m["lost"] += qty
                m["cost"] += unit_cost(item.seller_sku) * qty
                m["reasons"][reason] += qty
    out = [m for m in acc.values() if m["lost"]]
    for m in out:
        m["rate"] = m["lost"] / m["inspected"] if m["inspected"] else None
        m["reasons"] = m["reasons"].most_common()
    return sorted(out, key=lambda m: (-m["cost"], m["code"]))
