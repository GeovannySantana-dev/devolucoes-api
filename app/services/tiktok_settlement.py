# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Custo real das devoluções do TikTok, pelo extrato financeiro do pedido.

Enquanto a devolução está aberta, o app usa a previsão do frete que vem na própria devolução
(`seller_paid_return_shipping_fee`). Depois que ela fecha, o TikTok liquida o pedido (cerca de 1 dia) e o
extrato (`/finance/202501/orders/{id}/statement_transactions`) mostra o que de fato saiu do caixa:

- frete de devolução cobrado da loja (`return_shipping_fee_amount`);
- venda perdida: o frete de ida que a loja pagou (ou o desconto de frete que o TikTok tinha dado e
  estorna) e as taxas da venda que não são devolvidas (comissão, afiliado, taxa de serviço).

Pedido devolvido inteiro (receita líquida zero): a venda perdida é todo o resultado negativo do pedido
menos o frete de devolução. Devolução parcial: só as linhas de estorno do extrato contam, e as taxas
ficam de fora (não dá para separar por item com segurança).
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

log = logging.getLogger(__name__)

STATEMENT = "/finance/202501/orders/{order_id}/statement_transactions"
SETTLE_AFTER = timedelta(hours=12)  # o extrato demora um pouco para sair depois que a devolução fecha
RETRY_EVERY = timedelta(hours=6)
GIVE_UP_AFTER = timedelta(days=60)
CENT = Decimal("0.01")


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def costs_from_statement(data: dict[str, Any], returns_in_order: int = 1) -> tuple[Decimal, Decimal] | None:
    """(frete de devolução, venda perdida) em valores positivos; None se o pedido ainda não foi liquidado."""
    lines = data.get("sku_transactions") or []
    if not lines:
        return None
    breakdown = lambda line: line.get("shipping_cost_breakdown") or {}  # noqa: E731
    if _d(data.get("revenue_amount")) == 0:
        # Pedido devolvido inteiro: tudo o que o pedido deixou de saldo negativo é custo da devolução
        return_ship = -sum((_d(breakdown(x).get("return_shipping_fee_amount")) for x in lines), Decimal(0))
        lost = max(Decimal(0), -_d(data.get("settlement_amount")) - return_ship)
    else:
        refunds = [x for x in lines if _d(x.get("revenue_amount")) < 0 or _d(breakdown(x).get("return_shipping_fee_amount"))]
        return_ship = -sum((_d(breakdown(x).get("return_shipping_fee_amount")) for x in refunds), Decimal(0))
        lost = max(Decimal(0), -sum((_d(x.get("shipping_cost_amount")) for x in refunds), Decimal(0)) - return_ship)
    n = Decimal(max(1, returns_in_order))  # mais de uma devolução no mesmo pedido: divide igualmente
    return (return_ship / n).quantize(CENT), (lost / n).quantize(CENT)


def _pending(db: Session, limit: int) -> list[Return]:
    now = datetime.now(UTC)
    return list(
        db.scalars(
            select(Return)
            .where(
                Return.platform == Platform.TIKTOK,
                Return.kind == ReturnKind.CUSTOMER_RETURN,
                Return.status == ReturnStatus.COMPLETED,
                Return.cost_settled_at.is_(None),
                Return.external_order_id.is_not(None),
                Return.resolved_at < now - SETTLE_AFTER,
                Return.resolved_at > now - GIVE_UP_AFTER,
                or_(Return.cost_settle_checked_at.is_(None), Return.cost_settle_checked_at < now - RETRY_EVERY),
            )
            .options(selectinload(Return.items))
            .order_by(Return.resolved_at.desc())
            .limit(limit)
        )
    )


def settle_tiktok_costs(db: Session, limit: int = 60) -> dict[str, int]:
    """Lê o extrato das devoluções do TikTok já fechadas e grava o custo confirmado."""
    done = waiting = failed = 0
    clients: dict[Any, Any] = {}
    for ret in _pending(db, limit):
        store = db.get(Store, ret.store_id)
        try:
            if store.id not in clients:
                clients[store.id] = get_adapter(db, store).client
            data = clients[store.id].request(
                "GET", STATEMENT.format(order_id=ret.external_order_id), store.access_token, store.shop_cipher
            )
            n = db.scalar(
                select(func.count()).select_from(Return).where(
                    Return.store_id == ret.store_id, Return.external_order_id == ret.external_order_id,
                    Return.status != ReturnStatus.CANCELLED,
                )
            )
            result = costs_from_statement(data or {}, n or 1)
        except Exception as exc:  # uma falha não impede as demais
            log.warning("Extrato do pedido %s não lido: %s", ret.external_order_id, exc)
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
    return {"confirmadas": done, "aguardando_extrato": waiting - failed, "falhas": failed}
