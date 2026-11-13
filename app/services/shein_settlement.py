# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Custo real das devoluções da Shein, pelo extrato financeiro (`/finance/get-check-order-list`).

Cada lançamento do extrato tem um tipo (`secondOrderType`) e o número de negócio (`bzOrderNo`): o número do
pedido nos lançamentos da venda e o número da devolução nos da devolução. Visto nos dados reais da loja:

- 1 = receita do pedido: preço menos comissão e taxa de envio (ex.: 79,90 - 15,98 - 5,00 = 58,92);
- 2 = devolução do pedido: o que a Shein tira de volta (ex.: 79,90 - 15,98 = 63,92, a comissão volta mas a
  taxa de envio não);
- 13 = taxa de fulfillment da devolução (ex.: 10,00), mais 34/80/82 (débitos) e 84 (estorno) de frete de
  devolução;
- 12/14 = taxa de envio do pedido cobrada à parte / estornada.

Frete de devolução = débitos da devolução (13, 34, 80, 82) - estornos (84).
Venda perdida = o que a Shein tirou nas devoluções do pedido + taxa de envio à parte - receita do pedido (no
exemplo, 63,92 - 58,92 = 5,00, a taxa de envio que não volta). Devolução parcial dá negativo e conta zero.

A consulta só aceita janelas de até 7 dias pela data de geração do lançamento, então a rotina varre do pedido
mais antigo pendente até hoje, filtrando pelos números (até 100 por chamada).
"""

import logging
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from sqlalchemy import or_, select
from sqlalchemy.orm import Session

from ..enums import Platform, ReturnKind, ReturnStatus
from ..models import Return, Store
from ..platforms.registry import get_adapter
from ..platforms.shein.mapper import BEIJING
from .costs import compute_costs

log = logging.getLogger(__name__)

CHECK_LIST = "/open-api/finance/get-check-order-list"
WINDOW = timedelta(days=6, hours=23)  # a Shein recusa janelas de 7 dias ou mais
PAGE_SIZE = 30
IDS_PER_CALL = 100
BEFORE_REQUEST = timedelta(days=21)  # a receita do pedido sai antes de o cliente pedir a devolução
SETTLE_AFTER = timedelta(days=1)
RETRY_EVERY = timedelta(hours=6)
GIVE_UP_AFTER = timedelta(days=60)
CENT = Decimal("0.01")

ORDER_INCOME = 1
RETURN_DEBIT = 2
ORDER_FEE, ORDER_FEE_REFUND = 12, 14
RETURN_SHIPPING = {13, 34, 80, 82}
RETURN_SHIPPING_REFUND = {84}
TYPES = sorted({ORDER_INCOME, RETURN_DEBIT, ORDER_FEE, ORDER_FEE_REFUND, *RETURN_SHIPPING, *RETURN_SHIPPING_REFUND})


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def group_lines(lines: list[dict[str, Any]]) -> dict[str, dict[int, Decimal]]:
    """{bzOrderNo: {tipo: soma}} em valores positivos (o extrato já separa receita e despesa pelo tipo)."""
    out: dict[str, dict[int, Decimal]] = defaultdict(lambda: defaultdict(Decimal))
    seen: set[str] = set()
    for line in lines:
        if line.get("checkOrderNo") in seen:  # a mesma linha pode vir em duas janelas vizinhas
            continue
        seen.add(line.get("checkOrderNo"))
        out[str(line.get("bzOrderNo"))][int(line.get("secondOrderType") or 0)] += _d(line.get("estimateIncomeMoneyTotal"))
    return out


def shein_costs(
    by_no: dict[str, dict[int, Decimal]], return_no: str, order_no: str, returns_in_order: list[str]
) -> tuple[Decimal, Decimal] | None:
    """(frete de devolução, venda perdida) da devolução; None se o extrato ainda não tem os lançamentos."""
    ret_lines = by_no.get(return_no) or {}
    order_lines = by_no.get(order_no) or {}
    if RETURN_DEBIT not in ret_lines and not RETURN_SHIPPING & set(ret_lines):
        return None  # a Shein ainda não lançou a devolução
    if ORDER_INCOME not in order_lines:
        return None  # receita do pedido fora das janelas consultadas: tenta de novo depois
    shipping = sum((ret_lines.get(t, Decimal(0)) for t in RETURN_SHIPPING), Decimal(0)) - sum(
        (ret_lines.get(t, Decimal(0)) for t in RETURN_SHIPPING_REFUND), Decimal(0)
    )
    debits = sum(((by_no.get(no) or {}).get(RETURN_DEBIT, Decimal(0)) for no in returns_in_order), Decimal(0))
    fee = order_lines.get(ORDER_FEE, Decimal(0)) - order_lines.get(ORDER_FEE_REFUND, Decimal(0))
    lost = max(Decimal(0), debits + fee - order_lines[ORDER_INCOME])
    n = Decimal(max(1, len(returns_in_order)))  # mais de uma devolução no mesmo pedido: divide igualmente
    return max(Decimal(0), shipping).quantize(CENT), (lost / n).quantize(CENT)


def _pending(db: Session, limit: int) -> list[Return]:
    now = datetime.now(UTC)
    return list(
        db.scalars(
            select(Return)
            .where(
                Return.platform == Platform.SHEIN,
                Return.kind == ReturnKind.CUSTOMER_RETURN,
                Return.status == ReturnStatus.COMPLETED,
                Return.cost_settled_at.is_(None),
                Return.external_order_id.is_not(None),
                Return.resolved_at < now - SETTLE_AFTER,
                Return.resolved_at > now - GIVE_UP_AFTER,
                or_(Return.cost_settle_checked_at.is_(None), Return.cost_settle_checked_at < now - RETRY_EVERY),
            )
            .order_by(Return.resolved_at.desc())
            .limit(limit)
        )
    )


def fetch_lines(adapter, store: Store, numbers: list[str], start: datetime, end: datetime) -> list[dict[str, Any]]:
    fmt = lambda d: d.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
    lines: list[dict[str, Any]] = []
    for i in range(0, len(numbers), IDS_PER_CALL):
        chunk = numbers[i:i + IDS_PER_CALL]
        w_start = start
        while w_start < end:
            w_end = min(w_start + WINDOW, end)
            page = 1
            while True:
                info = adapter._call(store, CHECK_LIST, {
                    "page": page, "pageSize": PAGE_SIZE, "startAddTime": fmt(w_start), "endAddTime": fmt(w_end),
                    "bzOrderNos": chunk, "secondOrderTypes": TYPES,
                }) or {}
                rows = info.get("list") or []
                lines += rows
                if len(rows) < PAGE_SIZE:
                    break
                page += 1
            w_start = w_end
    return lines


def settle_shein_costs(db: Session, limit: int = 40) -> dict[str, int]:
    pending = _pending(db, limit)
    if not pending:
        return {"confirmadas": 0, "aguardando": 0}
    done = waiting = 0
    now = datetime.now(UTC)
    for store_id in {r.store_id for r in pending}:
        rets = [r for r in pending if r.store_id == store_id]
        store = db.get(Store, store_id)
        orders = {r.external_order_id for r in rets}
        # Todas as devoluções dos mesmos pedidos (a venda perdida é do pedido inteiro)
        siblings: dict[str, list[str]] = defaultdict(list)
        for no, order in db.execute(
            select(Return.external_return_id, Return.external_order_id).where(
                Return.store_id == store_id, Return.external_order_id.in_(orders), Return.status != ReturnStatus.CANCELLED
            )
        ):
            siblings[order].append(no)
        numbers = sorted(orders | {no for nos in siblings.values() for no in nos})
        start = min(r.requested_at or r.resolved_at for r in rets) - BEFORE_REQUEST
        by_no = group_lines(fetch_lines(get_adapter(db, store), store, numbers, start, now))
        for ret in rets:
            result = shein_costs(by_no, ret.external_return_id, ret.external_order_id,
                                 siblings.get(ret.external_order_id) or [ret.external_return_id])
            ret.cost_settle_checked_at = now
            if result is None:
                waiting += 1
            else:
                ret.settled_return_shipping, ret.settled_lost_sale = result
                ret.cost_settled_at = now
                compute_costs(ret)
                done += 1
        db.commit()
    return {"confirmadas": done, "aguardando": waiting}
