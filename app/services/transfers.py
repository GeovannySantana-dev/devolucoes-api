# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Lotes de transferência das peças de revenda entre a matriz (SP, conta 1 do Olist) e a filial (MS, conta 2).

Como o contador orientou: a NF de devolução entra na matriz, mas a peça fica fisicamente na filial (MS). Defeito e
item diferente não vão: ficam no depósito de defeitos da matriz.

Ida SP -> MS (tela /app/lotes):
1. "Fechar lote": junta as peças de revenda conferidas, com NF de devolução e estoque já no depósito de devoluções.
2. A NF de transferência é emitida no Olist da matriz SEM lançar estoque (o Olist lançaria no depósito de venda) e o
   número é informado no lote.
3. "Dar entrada no MS": para cada SKU, tira do depósito de devoluções de SP e lança no depósito da conta 2. Cada
   etapa fica registrada; repetir só faz o que faltou.

Volta MS -> SP (peças que voltam para venda). O app de bipagem da produção não mexe no estoque do Olist, então as
peças de devolução vão numa nota própria:
1. A operadora bipa as peças no app (SKU ou código de barras): cada uma é ligada à devolução mais antiga daquele SKU
   que está no MS.
2. "Fechar lote" e informar a NF de transferência da conta 2 (emitida SEM lançar estoque).
3. "Baixar no MS": tira as peças do depósito da conta 2.
4. "Entrada em SP": quando chegam, lança no depósito de venda da matriz. Só aí a peça volta a vender.
"""

import logging
from collections import OrderedDict
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..enums import EventType, ItemCondition
from ..erp.olist import OlistClient, OlistError
from ..models import Return, ReturnEvent, TransferBatch

log = logging.getLogger(__name__)

# NF de devolução já na matriz: autorizada pelo app ou emitida no Olist e vinculada (ex.: emissão em massa)
INVOICE_READY = ("authorized",)
STOCK_EXTERNAL = "external"
STOCK_POSTED = "posted"  # peça no depósito de devoluções de SP (sai de lá na ida)
# Estoque da devolução já resolvido na matriz: no depósito de devoluções, lançado fora do app ou produto excluído
STOCK_READY = (STOCK_POSTED, STOCK_EXTERNAL, "skipped_no_product")
TO_MS, TO_SP = "to_ms", "to_sp"


class TransferError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        self.status_code = status_code
        super().__init__(message)


def _resale_waiting():
    return [
        Return.received_at.is_not(None),
        Return.item_condition == ItemCondition.RESALE,
        Return.transfer_batch_id.is_(None),
        Return.handled_outside_at.is_(None),
    ]


def eligible(db: Session) -> list[Return]:
    """Peças de revenda prontas para o próximo lote (NF de devolução já na matriz)."""
    return list(db.scalars(
        select(Return)
        .where(*_resale_waiting(), Return.erp_stock_status.in_(STOCK_READY),
               (Return.erp_return_invoice_status.in_(INVOICE_READY)) | (Return.erp_stock_status == STOCK_EXTERNAL))
        .options(selectinload(Return.items))
        .order_by(Return.received_at)
    ))


def waiting_invoice(db: Session) -> int:
    """Conferidas como revenda ainda sem NF de devolução ou sem o estoque lançado: entram no lote depois."""
    return db.scalar(
        select(func.count()).select_from(Return).where(
            *_resale_waiting(), func.coalesce(Return.erp_stock_status, "").not_in(STOCK_READY),
        )
    ) or 0


def summarize(returns: list[Return]) -> list[dict[str, Any]]:
    """Peças por SKU: quantidade, nome, valor, pedidos de origem e quantas estão no depósito de devoluções de SP."""
    by_sku: "OrderedDict[str, dict[str, Any]]" = OrderedDict()
    for ret in returns:
        for item in ret.items:
            sku = (item.seller_sku or "SEM SKU").strip()
            row = by_sku.setdefault(sku, {"sku": sku, "name": item.product_name, "variant": item.sku_name,
                                          "quantity": 0, "amount": Decimal(0), "orders": [], "in_sp_deposit": 0})
            row["quantity"] += item.quantity or 1
            if ret.erp_stock_status == STOCK_POSTED:
                row["in_sp_deposit"] += item.quantity or 1
            row["amount"] += item.refund_amount or Decimal(0)
            if ret.external_order_id and ret.external_order_id not in row["orders"]:
                row["orders"].append(ret.external_order_id)
    return sorted(by_sku.values(), key=lambda r: r["sku"])


def close_batch(db: Session, actor: str | None) -> TransferBatch:
    returns = eligible(db)
    if not returns:
        raise TransferError("Nenhuma peça de revenda com NF de devolução esperando lote")
    rows = summarize(returns)
    batch = TransferBatch(
        status="closed", closed_by=actor, closed_at=datetime.now(UTC),
        pieces=sum(r["quantity"] for r in rows), amount=sum((r["amount"] for r in rows), Decimal(0)),
    )
    db.add(batch)
    db.flush()
    for ret in returns:
        ret.transfer_batch_id = batch.id
    db.commit()
    return batch


def set_invoice(db: Session, batch: TransferBatch, number: str) -> None:
    number = number.strip()
    if not number.isdigit():
        raise TransferError("Informe só o número da NF de transferência (ex.: 1001)", 422)
    batch.erp_invoice_number = number
    batch.erp_invoice_status = "informed"
    if batch.status == "closed":
        batch.status = "invoiced"
    batch.last_error = None
    db.commit()


def filial_client() -> OlistClient:
    s = get_settings()
    if not s.olist_filial_api_token:
        raise TransferError("Configure OLIST_FILIAL_API_TOKEN (token da conta 2 do Olist, filial MS)")
    return OlistClient(s.olist_filial_api_token, s.olist_api_base)


def matriz_client() -> OlistClient:
    s = get_settings()
    return OlistClient(s.olist_api_token, s.olist_api_base)


def _move(client: OlistClient, sku: str, qty: int, kind: str, deposit: str, note: str, account: str) -> None:
    """Entrada (E) ou saída (S) do SKU no depósito pelo nome (o Olist pode guardar o nome com espaço no fim)."""
    product_id = client.find_product_id_by_sku(sku)
    if not product_id:
        raise TransferError(f"SKU não cadastrado na {account} do Olist")
    exact = client.deposit_names(product_id).get(deposit.strip())
    if not exact:
        raise TransferError(f'Depósito "{deposit.strip()}" não existe no produto, na {account}')
    client.move_stock(product_id, qty, kind, exact, note)


def _run_steps(batch: TransferBatch, rows: list[dict[str, Any]], steps: list[tuple[str, Any]]) -> dict[str, Any]:
    """Executa as etapas por SKU, na ordem, guardando em ms_stock_result o que já foi feito.

    Cada etapa: (chave no resultado, função(row) que devolve a quantidade movida). Um SKU só passa para a etapa
    seguinte depois da anterior; repetir faz só o que faltou.
    """
    result = dict(batch.ms_stock_result or {})
    done = {key: dict(result.get(key) or {}) for key, _ in steps}
    errors: dict[str, str] = {}
    for row in rows:
        sku = row["sku"]
        for key, step in steps:
            if sku in done[key]:
                continue
            try:
                qty = step(row)
            except (OlistError, TransferError) as exc:
                errors[sku] = str(exc)[:200]
                break
            done[key][sku] = qty or 0
    result.update(done)
    result["errors"] = errors
    batch.ms_stock_result = result
    return result


def _finish(batch: TransferBatch, result: dict[str, Any], status: str, what: str, stamp: str) -> None:
    errors = result["errors"]
    if errors:
        batch.status = "error"
        batch.last_error = f"{len(errors)} SKU(s) sem {what}: " + "; ".join(f"{k}: {v}" for k, v in errors.items())
    else:
        batch.status = status
        setattr(batch, stamp, datetime.now(UTC))
        batch.last_error = None


def post_ms_stock(db: Session, batch: TransferBatch, client: OlistClient | None = None,
                  matriz: OlistClient | None = None) -> dict[str, Any]:
    """Ida: tira cada SKU do depósito de devoluções de SP e dá entrada no depósito da filial."""
    if not batch.erp_invoice_number:
        raise TransferError("Informe o número da NF de transferência antes de dar entrada no MS")
    s = get_settings()
    client = client or filial_client()
    matriz = matriz or matriz_client()
    note = f"Transferência para o MS - NF {batch.erp_invoice_number} (lote {batch.number} do app de devoluções)"

    def out_sp(row):
        qty = int(row.get("in_sp_deposit") or 0)
        if qty and s.olist_resale_deposit_name.strip():
            _move(matriz, row["sku"], qty, "S", s.olist_resale_deposit_name, note, "matriz (conta 1)")
        return qty

    def into_ms(row):
        _move(client, row["sku"], int(row["quantity"]), "E", s.olist_filial_deposit, note, "filial (conta 2)")
        return row["quantity"]

    result = _run_steps(batch, summarize(list(batch.returns)), [("sp_out", out_sp), ("ok", into_ms)])
    _finish(batch, result, "stocked", "entrada no MS", "ms_stock_posted_at")
    db.commit()
    return result


# ---------- volta MS -> SP ----------

def in_ms(db: Session) -> list[Return]:
    """Peças de revenda que entraram no MS por um lote de ida e ainda não voltaram."""
    return list(db.scalars(
        select(Return)
        .join(TransferBatch, Return.transfer_batch_id == TransferBatch.id)
        .where(TransferBatch.status == "stocked", Return.back_batch_id.is_(None),
               Return.item_condition == ItemCondition.RESALE, Return.handled_outside_at.is_(None))
        .options(selectinload(Return.items))
        .order_by(Return.received_at)
    ))


def open_back_batch(db: Session, actor: str | None) -> TransferBatch:
    """O lote de volta em bipagem, ou um novo."""
    batch = db.scalar(select(TransferBatch).where(TransferBatch.direction == TO_SP, TransferBatch.status == "open"))
    if batch:
        return batch
    batch = TransferBatch(direction=TO_SP, status="open", closed_by=actor, pieces=0, amount=Decimal(0))
    db.add(batch)
    db.commit()
    return batch


def current_back_batch(db: Session) -> TransferBatch | None:
    return db.scalar(select(TransferBatch).where(TransferBatch.direction == TO_SP, TransferBatch.status == "open"))


def _sku_of(code: str, available: list[Return], matriz: OlistClient | None) -> str | None:
    skus = {(i.seller_sku or "").strip().upper() for r in available for i in r.items}
    if code.upper() in skus:
        return code.upper()
    if matriz is None or not code.isdigit():
        return None
    try:  # código de barras (GTIN/EAN) da etiqueta: o Olist diz qual é o SKU
        ret = matriz.call("produtos.pesquisa.php", gtin=code)
    except OlistError:
        return None
    for p in (ret or {}).get("produtos") or []:
        codigo = str((p.get("produto") or {}).get("codigo") or "").strip().upper()
        if codigo in skus:
            return codigo
    return None


def _recount(batch: TransferBatch) -> None:
    rows = summarize(list(batch.back_returns))
    batch.pieces = sum(r["quantity"] for r in rows)
    batch.amount = sum((r["amount"] for r in rows), Decimal(0))


def scan(db: Session, batch: TransferBatch, code: str, matriz: OlistClient | None = None) -> Return:
    """Bipa uma peça: liga a devolução mais antiga daquele SKU que está no MS ao lote de volta."""
    if batch.status != "open":
        raise TransferError("Este lote já foi fechado: abra um novo para bipar")
    code = code.strip()
    if not code:
        raise TransferError("Bipe ou digite o SKU da peça", 422)
    available = in_ms(db)
    sku = _sku_of(code, available, matriz if matriz is not None else matriz_client())
    if not sku:
        raise TransferError(f"Nenhuma peça de devolução no MS com o código {code}")
    ret = next(r for r in available if any((i.seller_sku or "").strip().upper() == sku for i in r.items))
    ret.back_batch_id = batch.id
    batch.back_returns.append(ret)
    _recount(batch)
    db.commit()
    return ret


def unscan(db: Session, batch: TransferBatch, return_id: str) -> None:
    if batch.status != "open":
        raise TransferError("Este lote já foi fechado")
    for ret in list(batch.back_returns):
        if str(ret.id) == str(return_id):
            ret.back_batch_id = None
            batch.back_returns.remove(ret)
    _recount(batch)
    db.commit()


def close_back_batch(db: Session, batch: TransferBatch, actor: str | None) -> None:
    if batch.status != "open":
        raise TransferError("Este lote já foi fechado")
    if not batch.back_returns:
        raise TransferError("Bipe ao menos uma peça antes de fechar o lote")
    batch.status, batch.closed_by, batch.closed_at = "closed", actor, datetime.now(UTC)
    db.commit()


def ship_from_ms(db: Session, batch: TransferBatch, client: OlistClient | None = None) -> dict[str, Any]:
    """Volta, na saída do MS: tira as peças do depósito da filial."""
    if batch.status == "open":
        raise TransferError("Feche o lote antes de baixar no MS")
    if not batch.erp_invoice_number:
        raise TransferError("Informe o número da NF de transferência antes de baixar no MS")
    client = client or filial_client()
    deposit = get_settings().olist_filial_deposit
    note = f"Volta para SP - NF {batch.erp_invoice_number} (lote {batch.number} do app de devoluções)"

    def out_ms(row):
        _move(client, row["sku"], int(row["quantity"]), "S", deposit, note, "filial (conta 2)")
        return row["quantity"]

    result = _run_steps(batch, summarize(list(batch.back_returns)), [("ms_out", out_ms)])
    _finish(batch, result, "shipped", "baixa no MS", "ms_stock_posted_at")
    db.commit()
    return result


def receive_in_sp(db: Session, batch: TransferBatch, matriz: OlistClient | None = None,
                  actor: str | None = None) -> dict[str, Any]:
    """Volta, na chegada em SP: dá entrada das peças no depósito de venda da matriz."""
    if not batch.ms_stock_posted_at:
        raise TransferError("Baixe as peças no MS antes de dar entrada em SP")
    s = get_settings()
    if not s.olist_default_deposit_name.strip():
        raise TransferError("Configure OLIST_DEFAULT_DEPOSIT_NAME (depósito de venda da matriz)")
    matriz = matriz or matriz_client()
    note = f"Volta do MS para venda - NF {batch.erp_invoice_number} (lote {batch.number} do app de devoluções)"

    def in_sp(row):
        _move(matriz, row["sku"], int(row["quantity"]), "E", s.olist_default_deposit_name, note, "matriz (conta 1)")
        return row["quantity"]

    rets = list(batch.back_returns)
    result = _run_steps(batch, summarize(rets), [("sp_in", in_sp)])
    _finish(batch, result, "stocked", "entrada em SP", "sp_stock_posted_at")
    if batch.status == "stocked":
        for ret in rets:
            ret.events.append(ReturnEvent(event_type=EventType.ERP_STOCK_POSTED, source="erp", actor=actor,
                                          message=f"Voltou do MS para venda em SP (lote {batch.number})"))
    db.commit()
    return result
