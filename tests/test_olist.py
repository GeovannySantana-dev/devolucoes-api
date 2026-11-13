import json
from datetime import date
from urllib.parse import parse_qs

import httpx
import pytest

from app.config import get_settings
from app.erp.olist import OlistClient, OlistError, invoice_state, is_undelivered
from app.models import Return, ReturnItem
from app.services.erp import ErpError, build_return_invoice, classify

RULES = [
    ("defeito", "defect", 10),
    ("damaged", "defect", 10),
    ("tamanho", "resale", 50),
    ("size", "resale", 50),
    ("cor", "resale", 50),
    ("wrong", "resale", 60),
]

SALE_INVOICE = {
    "chave_acesso": "3" * 44,
    "cliente": {"nome": "Maria", "cpf_cnpj": "12345678909", "uf": "SP", "cidade": "São Paulo", "vazio": ""},
    "itens": [
        {"item": {"id_produto": 10, "codigo": "CAM-AZ-M", "descricao": "Camiseta Azul M", "unidade": "UN",
                  "ncm": "6109.10.00", "quantidade": "2", "valor_unitario": "59.90"}},
        {"item": {"id_produto": 11, "codigo": "CAL-PT-40", "descricao": "Calça Preta 40", "unidade": "UN",
                  "quantidade": "1", "valor_unitario": "129.90"}},
    ],
}


@pytest.mark.parametrize(
    "code,text,expected",
    [
        ("ecom_order_delivered_refund_and_return_reason_wrong_size", None, "resale"),
        (None, "Produto com defeito na costura", "defect"),
        ("item_damaged", "Wrong item and damaged", "defect"),  # defeito tem prioridade
        (None, "Cor diferente da foto", "resale"),
        (None, "Corrupção de embalagem", None),  # 'cor' só casa como palavra inteira
        (None, None, None),
    ],
)
def test_classify(code, text, expected):
    assert classify(code, text, RULES) == expected


def _return(condition="resale", items=(("CAM-AZ-M", 1),)):
    ret = Return(
        platform="tiktok",
        kind="customer_return",
        external_return_id="R1",
        external_order_id="576400000000000001",
        item_condition=condition,
        erp_sale_invoice_key="3" * 44,
    )
    ret.items = [ReturnItem(seller_sku=sku, quantity=q) for sku, q in items]
    return ret


def test_build_return_invoice_only_returned_items():
    nota = build_return_invoice(_return(), SALE_INVOICE, get_settings(), date(2026, 10, 13))
    assert nota["tipo"] == "E"
    assert nota["finalidade"] == 4
    assert nota["refNFe"] == "3" * 44
    assert nota["data_emissao"] == "13/10/2026"
    assert nota["numero_pedido_ecommerce"] == "576400000000000001"
    assert nota["cliente"] == {"nome": "Maria", "cpf_cnpj": "12345678909", "uf": "SP", "cidade": "São Paulo"}
    assert len(nota["itens"]) == 1
    item = nota["itens"][0]["item"]
    assert item["codigo"] == "CAM-AZ-M"
    assert item["quantidade"] == "1"
    assert item["valor_unitario"] == "59.90"
    assert item["ncm"] == "6109.10.00"
    assert item["id_produto"] == 10


def test_build_return_invoice_applies_proportional_sale_discount():
    # Caso real: NF 019688 com CO-CRU-G2 (99) + VM-VMIL-G2 (148), desconto 90 -> item devolvido vale 62,93
    venda = {
        **SALE_INVOICE,
        "valor_produtos": "247.00",
        "valor_desconto": "90.00",
        "itens": [
            {"item": {"codigo": "CO-CRU-G2", "descricao": "Conjunto", "unidade": "UN", "quantidade": "1", "valor_unitario": "99.00"}},
            {"item": {"codigo": "VM-VMIL-G2", "descricao": "Vestido", "unidade": "UN", "quantidade": "1", "valor_unitario": "148.00"}},
        ],
    }
    s = get_settings().model_copy(update={"olist_return_value_source": "sale_invoice"})
    nota = build_return_invoice(_return(items=(("CO-CRU-G2", 1),)), venda, s, date.today())
    assert nota["itens"][0]["item"]["valor_unitario"] == "62.93"

    # Com o valor do marketplace (padrão), usa o preço real da peça no TikTok: 77,90
    ret = _return(items=(("CO-CRU-G2", 1),))
    ret.items[0].raw = {"refund_amount": {"refund_subtotal": "77.9", "refund_total": "77.9"}}
    nota = build_return_invoice(ret, venda, get_settings(), date.today())
    assert nota["itens"][0]["item"]["valor_unitario"] == "77.90"

    # Trava: nunca acima do preço cheio da NF de venda
    ret.items[0].raw = {"refund_amount": {"refund_subtotal": "120.00"}}
    nota = build_return_invoice(ret, venda, get_settings(), date.today())
    assert nota["itens"][0]["item"]["valor_unitario"] == "99.00"


def test_build_return_invoice_uses_natureza_by_condition(monkeypatch):
    s = get_settings().model_copy(update={"olist_natureza_resale_id": 111, "olist_natureza_defect_id": 222})
    assert build_return_invoice(_return("resale"), SALE_INVOICE, s, date.today())["id_natureza_operacao"] == 111
    assert build_return_invoice(_return("defect"), SALE_INVOICE, s, date.today())["id_natureza_operacao"] == 222
    # Item diferente do pedido: também emite NF, com a natureza que não lança estoque
    assert build_return_invoice(_return("mismatch"), SALE_INVOICE, s, date.today())["id_natureza_operacao"] == 222


def test_build_return_invoice_rejects_unknown_sku():
    with pytest.raises(ErpError, match="XYZ"):
        build_return_invoice(_return(items=(("XYZ", 1),)), SALE_INVOICE, get_settings(), date.today())


def test_build_return_invoice_rejects_excess_quantity():
    with pytest.raises(ErpError, match="maior que a vendida"):
        build_return_invoice(_return(items=(("CAL-PT-40", 2),)), SALE_INVOICE, get_settings(), date.today())


def test_build_return_invoice_whole_order_when_no_items():
    nota = build_return_invoice(_return(items=()), SALE_INVOICE, get_settings(), date.today())
    assert {i["item"]["codigo"] for i in nota["itens"]} == {"CAM-AZ-M", "CAL-PT-40"}


@pytest.mark.parametrize(
    "situacao,expected",
    [("6", "authorized"), (7, "authorized"), ("Autorizada", "authorized"), ("1", "draft"), ("3", "cancelled")],
)
def test_invoice_state(situacao, expected):
    assert invoice_state({"situacao": situacao}) == expected


@pytest.mark.parametrize(
    "situacao,expected",
    [("Não Entregue", True), ("nao_entregue", True), (9, True), ("Entregue", False), ("Enviado", False)],
)
def test_is_undelivered(situacao, expected):
    assert is_undelivered(situacao) is expected


def test_build_invoice_for_undelivered_uses_retorno_natureza():
    ret = _return(items=())
    ret.kind = "undelivered"
    s = get_settings().model_copy(update={"olist_natureza_undelivered_id": 333, "olist_natureza_resale_id": 111})
    nota = build_return_invoice(ret, SALE_INVOICE, s, date.today())
    assert nota["natureza_operacao"] == "Retorno de mercadoria nao entregue ao destinatario"
    assert nota["id_natureza_operacao"] == 333
    assert nota["obs"].startswith("Retorno nao entregue")
    assert len(nota["itens"]) == 2  # pedido inteiro


def test_search_orders_paginates():
    calls = []

    def handler(req):
        page = int(parse_qs(req.content.decode())["pagina"][0])
        calls.append(page)
        return httpx.Response(200, json={"retorno": {"status": "OK", "numero_paginas": 2,
                                                     "pedidos": [{"pedido": {"id": page}}]}})

    ids = [p["id"] for p in _client(handler).search_orders(situacao="nao_entregue")]
    assert ids == [1, 2]
    assert calls == [1, 2]


class _FakeOlist:
    def __init__(self, notas, skus=("CAM-AZ-M",)):
        self.notas, self.skus = notas, skus

    def find_entry_invoices(self, numero):
        return self.notas

    def get_invoice(self, invoice_id):
        return {"itens": [{"item": {"codigo": s}} for s in self.skus]}


class _FakeDb:
    commits = 0

    def commit(self):
        self.commits += 1


def test_existing_manual_invoice_is_linked_not_duplicated():
    from app.services.erp import _link_existing_entry_invoice

    ret = _return()
    notas = [{"id": "300000002", "numero": "100101", "situacao": "6"}]
    assert _link_existing_entry_invoice(_FakeDb(), ret, _FakeOlist(notas), "teste") is True
    assert ret.erp_return_invoice_id == "300000002"
    assert ret.erp_return_invoice_status == "authorized"
    assert ret.erp_stock_status is None  # nota emitida fora não lança estoque: o app lança pela conferência


def test_mass_emitted_invoice_found_by_ml_pack_number():
    from app.services.erp import _link_existing_entry_invoice

    class ByNumber(_FakeOlist):
        def find_entry_invoices(self, numero):  # a emissão no Olist gravou o nº do pacote do ML
            return [{"id": "77", "numero": "1001", "situacao": "6"}] if numero == "2000010000000006" else []

    ret = _return()
    ret.external_order_id = "2000010000000007"
    ret.raw = {"order": {"pack_id": 2000010000000006}}
    assert _link_existing_entry_invoice(_FakeDb(), ret, ByNumber([]), None) is True
    assert ret.erp_return_invoice_number == "1001"


def test_invoice_of_same_order_with_other_piece_is_not_linked():
    # Pedido com duas devoluções de peças diferentes: a nota da outra peça não serve para esta
    from app.services.erp import _link_existing_entry_invoice

    ret = _return()
    client = _FakeOlist([{"id": "1", "numero": "300", "situacao": "1"}], skus=("OUTRA-PECA",))
    assert _link_existing_entry_invoice(_FakeDb(), ret, client, None) is False
    assert ret.erp_return_invoice_id is None


def test_return_total_never_exceeds_net_sale():
    # Caso real: NF 100105, 3 peças de 74,90 com desconto de 11,24 no total; devolvidas as 3
    venda = {
        **SALE_INVOICE,
        "valor_produtos": "224.70",
        "valor_desconto": "11.24",
        "itens": [{"item": {"codigo": s, "descricao": "Vestido", "unidade": "UN", "quantidade": "1", "valor_unitario": "74.90"}}
                  for s in ("A", "B", "C")],
    }
    ret = _return(items=(("A", 1), ("B", 1), ("C", 1)))
    for item in ret.items:  # a Shopee manda o preço cheio da peça
        item.raw = {"refund_amount": 74.90}
    from decimal import Decimal

    nota = build_return_invoice(ret, venda, get_settings(), date.today())
    total = sum(Decimal(i["item"]["valor_unitario"]) for i in nota["itens"])
    assert total <= Decimal("213.46") and total >= Decimal("213.43")


def test_cancelled_manual_invoice_is_ignored():
    from app.services.erp import _link_existing_entry_invoice

    ret = _return()
    assert _link_existing_entry_invoice(_FakeDb(), ret, _FakeOlist([{"id": "1", "situacao": "3"}]), None) is False
    assert ret.erp_return_invoice_id is None


def _client(handler):
    return OlistClient("tok", "https://olist.test/api2", http=httpx.Client(transport=httpx.MockTransport(handler)),
                       max_retries=1)


def test_client_sends_token_and_parses():
    seen = {}

    def handler(req):
        seen["path"] = req.url.path
        seen["form"] = parse_qs(req.content.decode())
        return httpx.Response(200, json={"retorno": {"status": "OK", "pedidos": [{"pedido": {"id": 99, "numero": 1}}]}})

    order = _client(handler).find_order_by_ecommerce_number("576400000000000001")
    assert order["id"] == 99
    assert seen["path"].endswith("/pedidos.pesquisa.php")
    assert seen["form"]["token"] == ["tok"]
    assert seen["form"]["formato"] == ["json"]
    assert seen["form"]["numeroEcommerce"] == ["576400000000000001"]


def test_client_no_records_returns_none():
    def handler(req):
        return httpx.Response(200, json={"retorno": {"status": "Erro", "codigo_erro": "20", "erros": [{"erro": "x"}]}})

    assert _client(handler).find_order_by_ecommerce_number("1") is None


def test_client_raises_on_error():
    def handler(req):
        return httpx.Response(200, json={"retorno": {"status": "Erro", "codigo_erro": 2, "erros": [{"erro": "Token inválido"}]}})

    with pytest.raises(OlistError, match="Token inválido"):
        _client(handler).get_order("1")


def test_create_invoice_accepts_registros_as_object():
    def handler(req):
        return httpx.Response(200, json={"retorno": {"status": "OK", "registros": {
            "registro": {"status": "OK", "id": "300000003", "numero": "100102", "serie": "4"}}}})

    assert _client(handler).create_invoice({"tipo": "E"})["numero"] == "100102"


def test_create_invoice_sends_json_nota():
    seen = {}

    def handler(req):
        seen["nota"] = json.loads(parse_qs(req.content.decode())["nota"][0])
        return httpx.Response(200, json={"retorno": {"status": "OK", "registros": [
            {"registro": {"status": "OK", "id": "555", "numero": "123", "serie": "1"}}]}})

    reg = _client(handler).create_invoice({"tipo": "E"})
    assert reg["id"] == "555"
    assert seen["nota"] == {"nota_fiscal": {"tipo": "E"}}


# ---------- natureza de devolução pelo CFOP da venda ----------

def _sale(cfops, regime="3"):
    itens = [{"item": {"codigo": f"SKU{i}", "descricao": f"Peça {i}", "unidade": "UN", "quantidade": "1",
                       "valor_unitario": "50.00", "cfop": c}} for i, c in enumerate(cfops)]
    return {"chave_acesso": "3" * 44, "numero": "12345", "regime_tributario": regime, "cliente": {"uf": "MG"},
            "itens": itens}


def _naturezas(**extra):
    return get_settings().model_copy(update={"olist_return_naturezas": "202:900, 201:901, 411:902, 202S:903",
                                             "olist_natureza_resale_id": 111, **extra})


@pytest.mark.parametrize("cfop,regime,expected", [
    ("5102", "3", 900), ("6108", "3", 900), ("6102", "3", 900),  # terceiros -> ?.202
    ("5101", "3", 901), ("6107", "3", 901),                     # produção própria -> ?.201
    ("5405", "3", 902), ("6404", "3", 902),                     # ST -> ?.411
    ("6108", "1", 903),                                         # venda no Simples -> sem destaque
])
def test_natureza_follows_sale_cfop(cfop, regime, expected):
    nota = build_return_invoice(_return(items=(("SKU0", 1),)), _sale([cfop], regime), _naturezas(), date.today())
    assert nota["id_natureza_operacao"] == expected and "natureza_operacao" not in nota
    assert nota["refNFe"] == "3" * 44 and "ref NF 12345" in nota["obs"]  # sempre referencia a NF de origem


def test_undelivered_follows_sale_cfop_unless_it_has_its_own_natureza():
    ret = _return(items=(("SKU0", 1),))
    ret.kind = "undelivered"
    nota = build_return_invoice(ret, _sale(["6108"]), _naturezas(), date.today())
    assert nota["id_natureza_operacao"] == 900 and nota["refNFe"] == "3" * 44
    assert nota["obs"].startswith("Retorno nao entregue")
    nota = build_return_invoice(ret, _sale(["6108"]), _naturezas(olist_natureza_undelivered_id=333), date.today())
    assert nota["id_natureza_operacao"] == 333


def test_sku_with_stray_whitespace_is_found_on_the_sale_invoice():
    nota = build_return_invoice(_return(items=(("\tSKU0 ", 1),)), _sale(["5102"]), _naturezas(), date.today())
    assert nota["itens"][0]["item"]["codigo"] == "SKU0"


def test_natureza_errors_are_explicit():
    s = _naturezas()
    with pytest.raises(ErpError, match="CFOP 5949"):
        build_return_invoice(_return(items=(("SKU0", 1),)), _sale(["5949"]), s, date.today())
    with pytest.raises(ErpError, match="tipos diferentes"):
        build_return_invoice(_return(items=(("SKU0", 1), ("SKU1", 1))), _sale(["5102", "5405"]), s, date.today())
    with pytest.raises(ErpError, match="Simples"):  # Simples x.201 não configurado
        build_return_invoice(_return(items=(("SKU0", 1),)), _sale(["5101"], "1"), s, date.today())


def test_without_natureza_map_keeps_old_behavior():
    s = get_settings().model_copy(update={"olist_return_naturezas": "", "olist_natureza_resale_id": 111})
    nota = build_return_invoice(_return(items=(("SKU0", 1),)), _sale(["5102"]), s, date.today())
    assert nota["id_natureza_operacao"] == 111


def test_shopee_item_raw_refund_is_number():
    from decimal import Decimal
    ret = _return(items=(("SKU0", 1),))
    ret.items[0].raw = {"refund_amount": 59.9, "item_id": 1}  # Shopee manda número, não objeto
    ret.items[0].refund_amount = Decimal("45.00")
    nota = build_return_invoice(ret, _sale(["5102"]), _naturezas(), date.today())
    assert nota["itens"][0]["item"]["valor_unitario"] == "45.00"


class _EntryClient:
    def __init__(self, notas, references=False, detail_skus=()):
        self.notas, self.references, self.detail_skus = notas, references, detail_skus

    def find_entry_invoices_by_document(self, document):
        assert document == "12345678909"
        return self.notas

    def invoice_references(self, invoice_id, sale_key):
        return self.references

    def get_invoice(self, invoice_id):
        return {"itens": [{"item": {"codigo": s}} for s in self.detail_skus]}


def _entry_case(client):
    from types import SimpleNamespace as NS

    from app.services import erp

    ret = NS(items=[NS(seller_sku="\tSKU0")], erp_sale_invoice_key="3" * 44, events=[], external_order_id="P1")
    db = NS(commit=lambda: None)
    sale = {"cliente": {"cpf_cnpj": "123.456.789-09"}}
    return erp._link_entry_invoice_of_sale(db, ret, client, sale, "Ana"), ret


@pytest.mark.parametrize("situacao,references,skus,linked", [
    ("6", True, ("SKU0",), True),     # autorizada que referencia a venda, com a peça: é a mesma devolução
    ("6", True, ("OUTRO",), False),   # mesma venda, outra peça: pedido com duas peças, uma nota para cada
    ("6", False, ("OUTRO",), False),  # autorizada de outra venda, outra peça: outra devolução
    ("1", False, ("SKU0",), True),    # pendente, sem XML: vale o mesmo SKU
    ("1", False, ("OUTRO",), False),
])
def test_mass_invoice_without_order_number_is_found_by_customer(monkeypatch, situacao, references, skus, linked):
    from app.services import erp

    monkeypatch.setattr(erp, "_event", lambda *a, **k: None)
    client = _EntryClient([{"id": "9", "numero": "100106", "situacao": situacao}], references, skus)
    found, ret = _entry_case(client)
    assert found is linked
    if linked:
        # A emissão em massa não lançou estoque: fica pendente para o app lançar quando estiver autorizada
        assert ret.erp_return_invoice_number == "100106" and ret.erp_stock_status is None


def test_authorized_invoice_of_other_sale_with_same_piece_stops(monkeypatch):
    from app.services import erp

    monkeypatch.setattr(erp, "_event", lambda *a, **k: None)
    client = _EntryClient([{"id": "9", "numero": "100107", "situacao": "6"}], False, ("SKU0",))
    with pytest.raises(erp.ErpError, match="100107"):
        _entry_case(client)


def test_invoice_xml_unavailable_raises_instead_of_not_referencing(monkeypatch):
    monkeypatch.setattr("app.erp.olist.time.sleep", lambda s: None)
    key = "3" * 44
    answers = iter(['{"retorno":{"status":"Erro","codigo_erro":"6"}}', f"<nfeProc><NFe><infNFe><chaveAcesso>{key}</chaveAcesso>"])
    client = _client(lambda req: httpx.Response(200, text=next(answers)))
    client.max_retries = 2
    assert client.invoice_references("9", key) is True  # tentou de novo depois do limite

    client = _client(lambda req: httpx.Response(200, text='{"retorno":{"status":"Erro"}}'))
    with pytest.raises(OlistError, match="indisponível"):
        client.invoice_references("9", key)


def test_entry_invoices_by_document_reads_every_page():
    pages = {"1": ["1"], "2": ["2"]}

    def handler(req):
        page = dict(httpx.QueryParams(req.content.decode())).get("pagina")
        notas = [{"nota_fiscal": {"id": i}} for i in pages[page]]
        return httpx.Response(200, json={"retorno": {"status": "OK", "numero_paginas": 2, "notas_fiscais": notas}})

    assert [n["id"] for n in _client(handler).find_entry_invoices_by_document("1")] == ["1", "2"]


def test_stock_is_moved_from_default_to_target_deposit(monkeypatch):
    # O Olist lança a entrada da nota no depósito padrão: o app transfere para o depósito de devoluções
    from types import SimpleNamespace as NS

    from app.services import erp

    calls = []

    class Client:
        def post_invoice_stock(self, invoice_id, deposit_id):
            calls.append(("lancar", invoice_id))

        def get_invoice(self, invoice_id):
            return {"itens": [{"item": {"codigo": "SKU0", "id_produto": "77", "quantidade": "2.00"}}]}

        def deposit_names(self, product_id):
            return {"deposito venda": "deposito venda",
                    "deposito devolucoes": "deposito devolucoes "}

        def move_stock(self, product_id, qty, kind, deposit, note):
            calls.append((kind, product_id, qty, deposit))

    s = get_settings().model_copy(update={
        "olist_post_stock": True, "olist_default_deposit_name": "deposito venda",
        "olist_resale_deposit_name": "deposito devolucoes", "olist_resale_deposit_id": 9})
    monkeypatch.setattr(erp, "get_settings", lambda: s)
    monkeypatch.setattr(erp, "get_olist_client", lambda: Client())
    monkeypatch.setattr(erp, "_event", lambda *a, **k: None)
    ret = NS(id=1, erp_stock_status=None, erp_return_invoice_status="authorized", item_condition="resale",
             erp_return_invoice_id="5", erp_return_invoice_number="100108", erp_last_error=None, erp_stock_posted_at=None)
    erp.post_stock(NS(commit=lambda: None, scalar=lambda q: 0), ret)
    # Sai do depósito de venda ANTES de lançar a nota: a peça nunca fica disponível para venda
    assert calls == [("S", "77", 2, "deposito venda"), ("lancar", "5"),
                     ("E", "77", 2, "deposito devolucoes ")]
    assert ret.erp_stock_status == "posted"

    # Retoma de onde parou, sem repetir a saída nem o lançamento
    calls.clear()
    ret.erp_stock_status = "reserved"
    erp.post_stock(NS(commit=lambda: None, scalar=lambda q: 0), ret)
    assert [c[0] for c in calls] == ["lancar", "E"]
    calls.clear()
    ret.erp_stock_status = "launched"
    erp.post_stock(NS(commit=lambda: None, scalar=lambda q: 0), ret)
    assert [c[0] for c in calls] == ["E"]


def _stock_case(monkeypatch, items, found_by_sku):
    from types import SimpleNamespace as NS

    from app.services import erp

    calls = []

    class Client:
        def post_invoice_stock(self, invoice_id, deposit_id):
            calls.append(("lancar",))

        def get_invoice(self, invoice_id):
            return {"itens": [{"item": i} for i in items]}

        def find_product_id_by_sku(self, sku):
            return found_by_sku.get(sku)

        def deposit_names(self, product_id):
            return {"deposito venda": "deposito venda",
                    "deposito devolucoes": "deposito devolucoes "}

        def move_stock(self, product_id, qty, kind, deposit, note):
            calls.append((kind, product_id))

    s = get_settings().model_copy(update={
        "olist_post_stock": True, "olist_default_deposit_name": "deposito venda",
        "olist_resale_deposit_name": "deposito devolucoes"})
    monkeypatch.setattr(erp, "get_settings", lambda: s)
    monkeypatch.setattr(erp, "get_olist_client", lambda: Client())
    monkeypatch.setattr(erp, "_event", lambda *a, **k: None)
    ret = NS(id=1, erp_stock_status=None, erp_return_invoice_status="authorized", item_condition="resale",
             erp_return_invoice_id="5", erp_return_invoice_number="1", erp_last_error=None, erp_stock_posted_at=None)
    erp.post_stock(NS(commit=lambda: None, scalar=lambda q: 0), ret)
    return calls, ret


def test_item_without_product_link_only_enters_target_deposit(monkeypatch):
    # id_produto 0: o lançamento da nota não mexe no saldo, então não pode sair nada do estoque de venda
    calls, ret = _stock_case(monkeypatch, [{"codigo": "SKU0", "id_produto": "0", "quantidade": "1"}], {"SKU0": "88"})
    assert calls == [("lancar",), ("E", "88")] and ret.erp_stock_status == "posted"


def test_deleted_product_is_skipped_not_retried(monkeypatch):
    calls, ret = _stock_case(monkeypatch, [{"codigo": "SKU0", "id_produto": "0", "quantidade": "1"}], {})
    assert calls == [] and ret.erp_stock_status == "skipped_no_product"


def test_sale_without_customer_document_stops_instead_of_creating(monkeypatch):
    from types import SimpleNamespace as NS

    from app.services import erp

    ret = NS(items=[NS(seller_sku="SKU0")], erp_sale_invoice_key="3" * 44, events=[], external_order_id="P1")
    with pytest.raises(erp.ErpError, match="sem CPF"):
        erp._link_entry_invoice_of_sale(NS(commit=lambda: None), ret, object(), {"cliente": {}}, None)


def test_pending_invoices_job_records_errors_and_continues(monkeypatch):
    from types import SimpleNamespace as NS

    from app.services import erp

    rets = [NS(id=1), NS(id=2)]
    db = NS(scalars=lambda stmt: NS(all=lambda: rets), rollback=lambda: None, commit=lambda: None)
    s = get_settings().model_copy(update={"olist_invoice_mode": "draft"})
    monkeypatch.setattr(erp, "get_settings", lambda: s)
    monkeypatch.setattr(erp.time, "sleep", lambda x: None)
    recorded = []
    monkeypatch.setattr(erp, "_record_error", lambda db, ret, exc: recorded.append(ret.id))

    def create(db, ret, actor):
        if ret.id == 1:
            raise erp.ErpError("Pedido sem NF de venda autorizada no Olist", 409)

    monkeypatch.setattr(erp, "create_return_invoice", create)
    assert erp.create_pending_invoices(db) == {"criadas": 1, "erros": 1}
    assert recorded == [1]  # sai da fila com o erro; a outra foi criada


def _auto_case(nota_itens, venda_itens, nota_extra=None, venda_extra=None, app_items=(("SKU0", 1),)):
    from types import SimpleNamespace as NS

    from app.services import erp

    nota = {"situacao": "1", "valor_produtos": "70.00", "natureza_operacao": "Devolução de venda de mercadorias - terceiros",
            "itens": [{"item": i} for i in nota_itens], **(nota_extra or {})}
    venda = {"regime_tributario": "3", "valor_produtos": "70.00", "valor_desconto": "0",
             "itens": [{"item": i} for i in venda_itens], **(venda_extra or {})}

    class Client:
        def get_invoice(self, invoice_id):
            return nota if invoice_id == "N" else venda

    ret = NS(erp_return_invoice_id="N", erp_sale_invoice_id="V", items=[NS(seller_sku=s, quantity=q) for s, q in app_items])
    return erp.check_return_invoice(Client(), ret)


def test_auto_check_passes_mirror_of_sale():
    item = {"codigo": "SKU0", "quantidade": "1", "valor_unitario": "70.00"}
    assert _auto_case([{**item, "cfop": "2202"}], [{**item, "cfop": "6108"}]) == []


@pytest.mark.parametrize("nota_item,nota_extra,expected", [
    ({"cfop": "1202"}, None, "não corresponde à venda"),                  # venda fora de SP com CFOP de SP
    ({"cfop": "2202", "valor_unitario": "80.00"}, {"valor_produtos": "80.00"}, "maior que a venda"),
    ({"cfop": "2202"}, {"natureza_operacao": "Devolução de venda - origem Simples"}, "regime"),
])
def test_auto_check_blocks_wrong_drafts(nota_item, nota_extra, expected):
    base = {"codigo": "SKU0", "quantidade": "1", "valor_unitario": "70.00"}
    problems = _auto_case([{**base, **nota_item}], [{**base, "cfop": "6108"}], nota_extra)
    assert any(expected in p for p in problems), problems


def test_undelivered_gets_sale_tracking_from_shopee(monkeypatch):
    # Shopee Xpress: o Olist não tem o rastreio, e a etiqueta do pacote que volta é a do envio da venda
    from types import SimpleNamespace as NS

    from app.platforms import registry
    from app.services import erp, tokens

    class Shopee:
        def get_tracking_number(self, token, shop_id, order_sn):
            assert (token, shop_id, order_sn) == ("tok", "99", "261025EXEMPL02")
            return "BR100000000002F"

    monkeypatch.setattr(registry, "get_shopee_client", lambda: Shopee())
    monkeypatch.setattr(tokens, "ensure_shopee_token", lambda db, store: "tok")
    store = NS(platform="shopee", external_shop_id="99")
    assert erp._marketplace_tracking(None, store, "261025EXEMPL02") == "BR100000000002F"
    assert erp._marketplace_tracking(None, NS(platform="tiktok", external_shop_id="1"), "1") is None


SALE_XML = """<?xml version="1.0" encoding="UTF-8"?>
<nfeProc xmlns="http://www.portalfiscal.inf.br/nfe"><NFe><infNFe Id="NFe35261012345678000190550040001001031636023217">
<ide><serie>4</serie><nNF>100103</nNF><dhEmi>2026-10-10T10:00:00-03:00</dhEmi></ide>
<emit><CRT>3</CRT></emit>
<dest><CPF>12345678909</CPF><xNome>Maria</xNome><enderDest><xLgr>Rua A</xLgr><nro>10</nro><xBairro>Centro</xBairro>
<xMun>Salvador</xMun><UF>BA</UF><CEP>40000000</CEP></enderDest></dest>
<det nItem="1"><prod><cProd>VM-AZCELE-G</cProd><cEAN>SEM GTIN</cEAN><xProd>Vestido</xProd><NCM>62044900</NCM>
<CFOP>6108</CFOP><uCom>UN</uCom><qCom>1.0000</qCom><vUnCom>77.22</vUnCom><vProd>77.22</vProd></prod>
<imposto><ICMS><ICMS00><orig>0</orig></ICMS00></ICMS></imposto></det>
<total><ICMSTot><vProd>77.22</vProd><vDesc>0.00</vDesc><vNF>77.22</vNF></ICMSTot></total>
</infNFe></NFe></nfeProc>"""


def test_sale_from_xml_matches_olist_format():
    from app.erp.nfe_xml import sale_from_xml

    sale = sale_from_xml(SALE_XML)
    assert sale["chave_acesso"] == "35261012345678000190550040001001031636023217"
    assert sale["regime_tributario"] == "3" and sale["numero"] == "100103"
    assert sale["cliente"]["cpf_cnpj"] == "12345678909" and sale["cliente"]["uf"] == "BA"
    item = sale["itens"][0]["item"]
    assert (item["codigo"], item["cfop"], item["ncm"], item["origem"]) == ("VM-AZCELE-G", "6108", "62044900", "0")
    assert "gtin_ean" not in item  # "SEM GTIN" não vai para a nota


def test_deleted_sale_is_loaded_from_xml_zip(tmp_path, monkeypatch):
    import zipfile
    from types import SimpleNamespace as NS

    from app.erp import nfe_xml
    from app.services import erp

    z = tmp_path / "vendas.zip"
    with zipfile.ZipFile(z, "w") as f:
        f.writestr("35261012345678000190550040001001031636023217-nfe.xml", SALE_XML)
    nfe_xml._index.cache_clear()
    s = get_settings().model_copy(update={"olist_sale_xml_zips": str(z)})
    monkeypatch.setattr(erp, "get_settings", lambda: s)

    class Client:
        def get_invoice(self, invoice_id):
            raise erp.OlistError("Olist erro 32: Nota Fiscal não localizada", 32)

        def find_product_id_by_sku(self, sku):
            return "77"

    ret = NS(erp_sale_invoice_id="1", erp_sale_invoice_key="35261012345678000190550040001001031636023217")
    sale = erp.load_sale_invoice(Client(), ret)
    assert sale["numero"] == "100103" and sale["itens"][0]["item"]["id_produto"] == "77"

    ret.erp_sale_invoice_key = "9" * 44  # sem XML: erro claro, nunca nota sem base
    with pytest.raises(erp.ErpError, match="XML"):
        erp.load_sale_invoice(Client(), ret)


def test_stock_not_posted_twice_for_shared_invoice(monkeypatch):
    from types import SimpleNamespace as NS

    from app.services import erp

    monkeypatch.setattr(erp, "_event", lambda *a, **k: None)
    monkeypatch.setattr(erp, "_stock_of_invoice_handled", lambda db, ret: True)
    monkeypatch.setattr(erp, "get_olist_client", lambda: pytest.fail("não pode mexer no estoque"))
    ret = NS(erp_stock_status=None, erp_return_invoice_status="authorized", erp_return_invoice_number="100109",
             erp_last_error="x")
    erp.post_stock(NS(commit=lambda: None, scalar=lambda q: 0), ret)
    assert ret.erp_stock_status == "external" and ret.erp_last_error is None
