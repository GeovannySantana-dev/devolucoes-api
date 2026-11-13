# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Comprador reincidente: quem já devolveu antes, para a equipe conferir o pacote com mais cuidado.

Só Shopee (usuário da conta) e Mercado Livre (id do comprador) informam quem é o comprador na devolução;
TikTok e Shein não. A chave fica em returns.buyer_key ("shopee:usuario", "mercado_livre:123") e nunca é
mostrada na tela: só o número de devoluções e quais foram.
"""

from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..models import Return

REPEAT_MIN = 3  # "reincidente" no relatório: 3 ou mais devoluções no período


def buyer_key(platform: str, raw: dict[str, Any] | None) -> str | None:
    raw = raw or {}
    if platform == "shopee":
        name = ((raw.get("user") or {}).get("username") or "").strip().lower()
        return f"shopee:{name}" if name else None
    if platform == "mercado_livre":
        buyer = ((raw.get("order") or {}).get("buyer") or {}).get("id")
        if not buyer:
            players = (raw.get("claim") or {}).get("players") or []
            buyer = next((p.get("user_id") for p in players if p.get("role") == "complainant"), None)
        return f"mercado_livre:{buyer}" if buyer else None
    return None


def history_counts(db: Session, keys: set[str]) -> dict[str, dict[str, int]]:
    """Para cada comprador: total de devoluções e quantas vieram com item diferente do pedido."""
    if not keys:
        return {}
    rows = db.execute(
        select(
            Return.buyer_key,
            func.count(),
            func.count().filter(Return.item_condition == "mismatch"),
        )
        .where(Return.buyer_key.in_(keys))
        .group_by(Return.buyer_key)
    ).all()
    return {k: {"total": total, "mismatch": mismatch} for k, total, mismatch in rows}


def others(db: Session, ret: Return, limit: int = 20) -> list[Return]:
    """As outras devoluções do mesmo comprador, mais recentes primeiro."""
    if not ret.buyer_key:
        return []
    return list(db.scalars(
        select(Return)
        .where(Return.buyer_key == ret.buyer_key, Return.id != ret.id)
        .options(selectinload(Return.items))
        .order_by(Return.requested_at.desc().nulls_last())
        .limit(limit)
    ))


def repeat_buyers(db: Session, conds: list) -> list[dict[str, Any]]:
    """Compradores com REPEAT_MIN ou mais devoluções que atendem aos filtros (período, marketplace, loja)."""
    conds = [Return.buyer_key.is_not(None), *conds]
    keys = db.scalars(
        select(Return.buyer_key).where(*conds).group_by(Return.buyer_key).having(func.count() >= REPEAT_MIN)
    ).all()
    if not keys:
        return []
    rets = db.scalars(
        select(Return).where(*conds, Return.buyer_key.in_(keys))
        .options(selectinload(Return.items)).order_by(Return.requested_at.desc())
    ).all()
    grouped: dict[str, list[Return]] = {}
    for r in rets:
        grouped.setdefault(r.buyer_key, []).append(r)
    out = [
        {"platform": items[0].platform, "returns": items,
         "mismatch": sum(1 for r in items if r.item_condition == "mismatch")}
        for items in grouped.values()
    ]
    return sorted(out, key=lambda b: (b["mismatch"], len(b["returns"])), reverse=True)


def summary(db: Session, conds: list) -> dict[str, int]:
    keys = db.execute(
        select(Return.buyer_key, func.count().filter(Return.item_condition == "mismatch"))
        .where(Return.buyer_key.is_not(None), *conds)
        .group_by(Return.buyer_key)
        .having(func.count() >= REPEAT_MIN)
    ).all()
    return {"buyers": len(keys), "with_mismatch": sum(1 for _, mis in keys if mis)}
