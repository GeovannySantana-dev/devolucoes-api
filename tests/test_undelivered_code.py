"""Registro de pacote não entregue a partir da etiqueta (chave da NF, pedido do marketplace ou do Olist)."""

import pytest

from app.models import Return, Store
from app.services import erp

KEY = "50261012345678000190550010001001041123456789"  # nº da NF = posições 26-34 -> 100104
ORDER = {"id": "300000001", "numero": "10001", "numero_ecommerce": "2610107EXEMPLO",
         "ecommerce": {"id": "90001", "nomeEcommerce": "Shopee", "numeroPedidoEcommerce": "2610107EXEMPLO"}}


class FakeOlist:
    def __init__(self, invoices=(), by_mkt=None, by_number=None):
        self.invoices, self.by_mkt, self.by_number = list(invoices), by_mkt or {}, by_number or {}
        self.asked = []

    def find_sale_invoices_by_number(self, numero):
        self.asked.append(("nf", numero))
        return self.invoices

    def find_order_by_ecommerce_number(self, n):
        return self.by_mkt.get(n)

    def find_order_by_number(self, n):
        return self.by_number.get(n)

    def get_order(self, order_id):
        return ORDER if order_id == ORDER["id"] else None


def test_invoice_key_finds_order_and_skips_other_series():
    client = FakeOlist(
        invoices=[{"chave_acesso": "9" * 44, "numero_ecommerce": "OUTRA"},  # outra série, mesmo número
                  {"chave_acesso": KEY, "numero_ecommerce": "2610107EXEMPLO"}],
        by_mkt={"2610107EXEMPLO": {"id": "300000001"}},
    )
    assert erp.find_order_for_code(client, f" {KEY[:22]} {KEY[22:]} ") == ORDER  # espaços da leitura ignorados
    assert client.asked == [("nf", "100104")]


def test_marketplace_and_olist_numbers():
    assert erp.find_order_for_code(FakeOlist(by_mkt={"2610107EXEMPLO": {"id": "300000001"}}), "2610107EXEMPLO") == ORDER
    assert erp.find_order_for_code(FakeOlist(by_number={"10001": {"id": "300000001"}}), "10001") == ORDER
    assert erp.find_order_for_code(FakeOlist(), "nada") is None


class _DB:
    def __init__(self, existing=None, store=None):
        self.existing, self.store, self.calls = existing, store, 0

    def scalar(self, stmt):
        self.calls += 1
        return self.existing if self.calls == 1 else self.store


def test_register_reuses_existing_return(monkeypatch):
    existing = Return(external_order_id="2610107EXEMPLO")
    monkeypatch.setattr(erp, "get_olist_client", lambda: FakeOlist(by_mkt={"2610107EXEMPLO": {"id": "300000001"}}))
    ret, created = erp.register_undelivered_by_code(_DB(existing=existing), "2610107EXEMPLO", "Ana")
    assert ret is existing and created is False


def test_register_maps_store_by_olist_channel(monkeypatch):
    store = Store(name="Loja Exemplo", platform="shopee", erp_ecommerce_id="90001")
    made = []
    monkeypatch.setattr(erp, "get_olist_client", lambda: FakeOlist(by_mkt={"2610107EXEMPLO": {"id": "300000001"}}))
    monkeypatch.setattr(erp, "create_undelivered_from_order",
                        lambda db, st, order, source, actor, notes: made.append((st, order, actor, notes)) or Return())
    _, created = erp.register_undelivered_by_code(_DB(store=store), "2610107EXEMPLO", "Ana", "endereço não encontrado")
    assert created and made[0][0] is store and made[0][3] == "endereço não encontrado"

    with pytest.raises(erp.ErpError, match="sem loja cadastrada"):
        erp.register_undelivered_by_code(_DB(store=None), "2610107EXEMPLO", "Ana")
    monkeypatch.setattr(erp, "get_olist_client", lambda: FakeOlist())
    with pytest.raises(erp.ErpError, match="não encontrado"):
        erp.register_undelivered_by_code(_DB(), "xyz", "Ana")
