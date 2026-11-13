"""Lotes de transferência SP -> MS: resumo por SKU e entrada no estoque da filial (conta 2 do Olist)."""

from decimal import Decimal

import pytest

from app.erp.olist import OlistError
from app.models import Return, ReturnItem, TransferBatch
from app.services import transfers


def test_summary_groups_by_sku_with_orders():
    rows = transfers.summarize([_ret("A1", ("VC-REG-CRU-G1", 1, "79.90")), _ret("B2", ("VC-REG-CRU-G1", 2, "150"), ("VM-AZMAR-M", 1, "89"))])
    assert [(r["sku"], r["quantity"], r["amount"], r["orders"]) for r in rows] == [
        ("VC-REG-CRU-G1", 3, Decimal("229.90"), ["A1", "B2"]), ("VM-AZMAR-M", 1, Decimal("89"), ["B2"])]


class _DB:
    def commit(self):
        pass


class _Olist:
    """Conta do Olist de mentira: produtos pelo SKU e depósitos pelo nome."""

    def __init__(self, deposits=("Geral",), missing=(), failing=(), gtin=None):
        self.deposits, self.missing, self.failing, self.gtin = deposits, set(missing), set(failing), gtin or {}
        self.moves = []

    def find_product_id_by_sku(self, sku):
        return None if sku in self.missing else f"id-{sku}"

    def deposit_names(self, product_id):
        return {d.strip(): d for d in self.deposits}

    def move_stock(self, product_id, quantity, kind, deposit, note):
        if product_id.removeprefix("id-") in self.failing:
            raise OlistError("Olist fora")
        self.moves.append((kind, product_id, quantity, deposit))

    def call(self, method, **params):
        sku = self.gtin.get(params.get("gtin"))
        return {"produtos": [{"produto": {"codigo": sku}}]} if sku else {"produtos": []}


def _ret(order, *items, stock="posted"):
    r = Return(external_order_id=order, erp_stock_status=stock, items=[
        ReturnItem(seller_sku=sku, product_name="Vestido", quantity=qty, refund_amount=Decimal(value)) for sku, qty, value in items])
    return r


def _batch():
    b = TransferBatch(number=7, erp_invoice_number="1001", status="invoiced")
    b.returns = [_ret("A1", ("SKU-1", 2, "100")), _ret("B2", ("SKU-2", 1, "50")),
                 _ret("C3", ("SKU-2", 1, "50"), stock="skipped_no_product")]
    return b


@pytest.fixture(autouse=True)
def _settings(monkeypatch):
    s = transfers.get_settings().model_copy(update={
        "olist_resale_deposit_name": "deposito devolucoes", "olist_filial_deposit": "Geral",
        "olist_default_deposit_name": "deposito venda"})
    monkeypatch.setattr(transfers, "get_settings", lambda: s)


def test_ms_stock_needs_invoice_number():
    with pytest.raises(transfers.TransferError):
        transfers.post_ms_stock(_DB(), TransferBatch(number=1), _Olist(), _Olist())


def test_ida_takes_from_sp_returns_deposit_then_enters_ms_and_retries_only_missing():
    b = _batch()
    sp, ms = _Olist(deposits=("deposito devolucoes ", "deposito venda")), _Olist(failing={"SKU-2"})
    transfers.post_ms_stock(_DB(), b, ms, sp)
    # Sai do depósito de devoluções pelo nome exato (com espaço no fim); produto excluído não sai de SP
    assert sp.moves == [("S", "id-SKU-1", 2, "deposito devolucoes "),
                        ("S", "id-SKU-2", 1, "deposito devolucoes ")]
    assert ms.moves == [("E", "id-SKU-1", 2, "Geral")]
    assert b.status == "error" and "SKU-2" in b.last_error
    ms.failing.clear()
    transfers.post_ms_stock(_DB(), b, ms, sp)  # repetir faz só o que faltou: não tira de SP de novo
    assert len(sp.moves) == 2 and ms.moves[-1] == ("E", "id-SKU-2", 2, "Geral") and b.status == "stocked"


def test_ida_stops_sku_before_ms_when_sp_fails():
    b, sp, ms = _batch(), _Olist(deposits=("deposito devolucoes",), failing={"SKU-1"}), _Olist()
    transfers.post_ms_stock(_DB(), b, ms, sp)
    assert ("E", "id-SKU-1", 2, "Geral") not in ms.moves and b.status == "error"


def test_volta_scan_picks_oldest_return_of_the_sku(monkeypatch):
    old, new, other = _ret("OLD", ("VC-BRNC-M", 1, "68")), _ret("NEW", ("VC-BRNC-M", 1, "68")), _ret("X", ("VM-1", 1, "50"))
    monkeypatch.setattr(transfers, "in_ms", lambda db: [old, new, other])
    batch = TransferBatch(number=3, direction="to_sp", status="open")
    assert transfers.scan(_DB(), batch, " vc-brnc-m ", _Olist()) is old
    assert batch.pieces == 1 and batch.amount == Decimal("68")
    monkeypatch.setattr(transfers, "in_ms", lambda db: [new, other])
    assert transfers.scan(_DB(), batch, "7890000000017", _Olist(gtin={"7890000000017": "VM-1"})) is other
    with pytest.raises(transfers.TransferError, match="Nenhuma"):
        transfers.scan(_DB(), batch, "NAO-EXISTE", _Olist())


def test_volta_ships_from_ms_then_enters_sp_sale_deposit():
    batch = TransferBatch(number=4, direction="to_sp", status="closed", erp_invoice_number="88")
    batch.back_returns = [_ret("A", ("SKU-1", 1, "50")), _ret("B", ("SKU-1", 1, "50"))]
    with pytest.raises(transfers.TransferError):
        transfers.receive_in_sp(_DB(), batch, _Olist())  # ainda não saiu do MS
    ms, sp = _Olist(), _Olist(deposits=("deposito venda",))
    transfers.ship_from_ms(_DB(), batch, ms)
    assert ms.moves == [("S", "id-SKU-1", 2, "Geral")] and batch.status == "shipped"
    transfers.receive_in_sp(_DB(), batch, sp, actor="Ana")
    assert sp.moves == [("E", "id-SKU-1", 2, "deposito venda")] and batch.status == "stocked"
    assert all(r.events and "Voltou do MS" in r.events[-1].message for r in batch.back_returns)


def test_volta_cannot_ship_open_batch():
    with pytest.raises(transfers.TransferError, match="Feche"):
        transfers.ship_from_ms(_DB(), TransferBatch(number=5, direction="to_sp", status="open", erp_invoice_number="1"), _Olist())


def test_invoice_number_must_be_digits():
    with pytest.raises(transfers.TransferError):
        transfers.set_invoice(_DB(), TransferBatch(number=1, status="closed"), "NF abc")
