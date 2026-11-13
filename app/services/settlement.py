# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Custo confirmado das devoluções da Shopee e do Mercado Livre, pelo financeiro de cada plataforma.

Mesma ideia do TikTok (ver tiktok_settlement): enquanto a devolução está aberta o custo é estimado; depois que
ela fecha, o app lê o que de fato saiu do caixa e grava em `settled_return_shipping` (frete de devolução) e
`settled_lost_sale` (frete de ida e taxas da venda que não voltaram).

- Shopee: `payment/get_escrow_detail` do pedido. `reverse_shipping_fee` é o frete de devolução cobrado da
  loja; pedido devolvido inteiro (repasse <= 0): o resto do saldo negativo é a venda perdida (frete de ida).
  Devolução parcial (repasse > 0): só o frete de devolução.
- Mercado Livre: lançamentos do faturamento do pedido (`billing/integration/.../details`), cobranças menos
  estornos (BONUS). O que não foi estornado é a venda perdida; lançamentos de devolução, se houver, são o
  frete de devolução. Sem eles, vale o custo de devolução que a reclamação informa (`return_cost`).
"""

import logging
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session, selectinload

from ..enums import Platform, ReturnKind, ReturnStatus
from ..models import Return, Store
from ..platforms.registry import get_adapter
from .costs import compute_costs
from .shein_settlement import settle_shein_costs
from .tiktok_settlement import settle_tiktok_costs

log = logging.getLogger(__name__)
CENT = Decimal("0.01")
RETRY_EVERY = timedelta(hours=6)
GIVE_UP_AFTER = timedelta(days=60)
# O financeiro demora para fechar: Shopee calcula o repasse no reembolso; o faturamento do ML lança o estorno depois
SETTLE_AFTER = {Platform.SHOPEE: timedelta(hours=24), Platform.MERCADO_LIVRE: timedelta(days=5)}
ESCROW = "/api/v2/payment/get_escrow_detail"
ML_PERIODS = "/billing/integration/monthly/periods?group=ML&document_type=BILL&limit=3"
ML_DETAILS = "/billing/integration/periods/key/{key}/group/ML/details?document_type=BILL&limit=100&order_ids={order}"


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def _split(return_ship: Decimal, lost: Decimal, n: int) -> tuple[Decimal, Decimal]:
    k = Decimal(max(1, n))  # mais de uma devolução no mesmo pedido: divide igualmente
    return (return_ship / k).quantize(CENT), (max(Decimal(0), lost) / k).quantize(CENT)


def shopee_costs(order_income: dict[str, Any] | None, n: int = 1) -> tuple[Decimal, Decimal] | None:
    if not order_income or order_income.get("buyer_total_amount") is None:
        return None  # pedido ainda sem repasse calculado
    reverse = _d(order_income.get("reverse_shipping_fee"))
    escrow = _d(order_income.get("escrow_amount_after_adjustment", order_income.get("escrow_amount")))
    lost = -escrow - reverse if escrow <= 0 else Decimal(0)
    return _split(reverse, lost, n)


def ml_costs(details: list[dict[str, Any]], claim_return_cost: Decimal, n: int = 1) -> tuple[Decimal, Decimal] | None:
    if not details:
        return None  # faturamento ainda sem lançamentos do pedido
    net = Decimal(0)
    return_ship = Decimal(0)
    for d in details:
        info = d.get("charge_info") or {}
        amount = _d(info.get("detail_amount"))
        signed = -amount if str(info.get("detail_type")).upper() == "BONUS" else amount
        net += signed
        if "devolu" in str(info.get("transaction_detail") or "").lower():
            return_ship += signed
    if return_ship <= 0:  # o faturamento não separa o frete de devolução: vale o da reclamação
        return_ship = claim_return_cost
        lost = net
    else:
        lost = net - return_ship
    return _split(return_ship, lost, n)


def _pending(db: Session, platform: str, limit: int) -> list[Return]:
    now = datetime.now(UTC)
    return list(
        db.scalars(
            select(Return)
            .where(
                Return.platform == platform,
                Return.kind == ReturnKind.CUSTOMER_RETURN,
                Return.status == ReturnStatus.COMPLETED,
                Return.cost_settled_at.is_(None),
                Return.external_order_id.is_not(None),
                Return.resolved_at < now - SETTLE_AFTER[platform],
                Return.resolved_at > now - GIVE_UP_AFTER,
                or_(Return.cost_settle_checked_at.is_(None), Return.cost_settle_checked_at < now - RETRY_EVERY),
            )
            .options(selectinload(Return.items))
            .order_by(Return.resolved_at.desc())
            .limit(limit)
        )
    )


def _returns_in_order(db: Session, ret: Return) -> int:
    return db.scalar(
        select(func.count()).select_from(Return).where(
            Return.store_id == ret.store_id, Return.external_order_id == ret.external_order_id,
            Return.status != ReturnStatus.CANCELLED,
        )
    ) or 1


def _settle(db: Session, platform: str, limit: int, fetch) -> dict[str, int]:
    done = waiting = failed = 0
    ctx: dict[Any, Any] = {}
    for ret in _pending(db, platform, limit):
        store = db.get(Store, ret.store_id)
        try:
            result = fetch(db, store, ret, ctx)
        except Exception as exc:  # uma falha não impede as demais
            log.warning("Financeiro do pedido %s (%s) não lido: %s", ret.external_order_id, platform, exc)
            failed += 1
            result = None
        ret.cost_settle_checked_at = datetime.now(UTC)
        if result is None:
            waiting += 1
        else:
            ret.settled_return_shipping, ret.settled_lost_sale = result
            ret.cost_settled_at = datetime.now(UTC)
            compute_costs(ret)
            done += 1
        db.commit()
    return {"confirmadas": done, "aguardando": waiting - failed, "falhas": failed}


def _fetch_shopee(db: Session, store: Store, ret: Return, ctx: dict) -> tuple[Decimal, Decimal] | None:
    if store.id not in ctx:
        adapter = get_adapter(db, store)
        ctx[store.id] = (adapter.client, *adapter._auth(store))
    client, token, shop_id = ctx[store.id]
    data = client.request("GET", ESCROW, token, shop_id, query={"order_sn": ret.external_order_id})
    income = (data.get("response") or data or {}).get("order_income")
    return shopee_costs(income, _returns_in_order(db, ret))


def _fetch_ml(db: Session, store: Store, ret: Return, ctx: dict) -> tuple[Decimal, Decimal] | None:
    if store.id not in ctx:
        adapter = get_adapter(db, store)
        token = adapter.token_provider(store)
        periods = adapter.client.request("GET", ML_PERIODS, token).get("results") or []
        ctx[store.id] = (adapter.client, token, [p["key"] for p in periods])
    client, token, keys = ctx[store.id]
    details: list[dict[str, Any]] = []
    for key in keys:
        data = client.request("GET", ML_DETAILS.format(key=key, order=ret.external_order_id), token)
        details += data.get("results") or []
    claim_cost = _d(((ret.raw or {}).get("return_cost") or {}).get("amount"))
    return ml_costs(details, claim_cost, _returns_in_order(db, ret))


def settle_all_costs(db: Session, limit: int = 60) -> dict[str, dict[str, int]]:
    """Rotina: confirma pelo financeiro de cada plataforma o custo das devoluções já fechadas."""
    out: dict[str, dict[str, int]] = {}
    for name, fn in (
        ("tiktok", lambda: settle_tiktok_costs(db, limit)),
        ("shopee", lambda: _settle(db, Platform.SHOPEE, limit, _fetch_shopee)),
        ("mercado_livre", lambda: _settle(db, Platform.MERCADO_LIVRE, limit, _fetch_ml)),
        ("shein", lambda: settle_shein_costs(db)),
    ):
        try:
            out[name] = fn()
        except Exception as exc:
            db.rollback()
            log.exception("Extrato %s falhou", name)
            out[name] = {"erro": str(exc)[:200]}
    return out
