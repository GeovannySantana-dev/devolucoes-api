"""Cliente da API v2 do Olist ERP (antigo Tiny).

Docs: https://api-docs.erp.olist.com/api-v2
Todas as chamadas são POST com `token` e `formato=json`; a resposta vem em `retorno`.
"""

import html
import json
import logging
import time
import unicodedata
from collections.abc import Iterator
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Código de erro do Olist para "a consulta não retornou registros"
NO_RECORDS = 20
# Código de erro para limite de requisições excedido
RATE_LIMITED = 6


class OlistError(Exception):
    def __init__(self, message: str, code: int | None = None):
        self.code = code
        super().__init__(message)


class OlistClient:
    def __init__(self, token: str, base_url: str, http: httpx.Client | None = None, max_retries: int = 3):
        self.token = token
        self.base_url = base_url.rstrip("/")
        self.http = http or httpx.Client(timeout=30)
        self.max_retries = max_retries

    def call(self, endpoint: str, **params: Any) -> dict[str, Any] | None:
        """Chama o endpoint e devolve `retorno`. None quando o Olist responde 'sem registros'."""
        if not self.token:
            raise OlistError("OLIST_API_TOKEN não configurado")
        data = {"token": self.token, "formato": "json"}
        data.update({k: v if isinstance(v, str) else str(v) for k, v in params.items() if v is not None})

        for attempt in range(1, self.max_retries + 1):
            try:
                resp = self.http.post(f"{self.base_url}/{endpoint}", data=data)
            except httpx.TransportError as exc:
                if attempt == self.max_retries:
                    raise OlistError(f"Falha de conexão com Olist: {exc}") from exc
                time.sleep(2**attempt)
                continue

            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == self.max_retries:
                    raise OlistError(f"Olist HTTP {resp.status_code}")
                time.sleep(2**attempt)
                continue

            ret = resp.json().get("retorno", {})
            if ret.get("status") == "OK":
                return ret

            code = _int(ret.get("codigo_erro"))
            if code == NO_RECORDS:
                return None
            if code == RATE_LIMITED and attempt < self.max_retries:
                time.sleep(10 * attempt)
                continue
            erros = "; ".join(e.get("erro", "") for e in ret.get("erros") or [] if isinstance(e, dict))
            raise OlistError(f"Olist erro {code}: {erros or ret}", code)

        raise OlistError("Tentativas esgotadas")

    # ---------- produtos ----------

    def find_sku_by_gtin(self, gtin: str) -> str | None:
        """SKU (código) do produto pelo código de barras da etiqueta da peça (EAN/GTIN)."""
        ret = self.call("produtos.pesquisa.php", pesquisa="", gtin=gtin)
        for p in (ret or {}).get("produtos") or []:
            prod = p.get("produto") or {}
            if str(prod.get("gtin") or "") == gtin and prod.get("codigo"):
                return prod["codigo"]
        return None

    def find_product_id_by_sku(self, sku: str) -> str | None:
        """ID do produto pelo SKU (código), só com correspondência exata."""
        ret = self.call("produtos.pesquisa.php", pesquisa=sku)
        for p in (ret or {}).get("produtos") or []:
            prod = p.get("produto") or {}
            if str(prod.get("codigo") or "").strip().upper() == sku.strip().upper():
                return str(prod["id"])
        return None

    def add_stock(self, product_id: str, quantity: int, deposit: str, note: str) -> dict[str, Any]:
        """Entrada (tipo E) no estoque do produto, no depósito informado pelo nome."""
        estoque = {"estoque": {"idProduto": product_id, "tipo": "E", "quantidade": quantity,
                               "deposito": deposit, "observacoes": note}}
        return self.call("produto.atualizar.estoque.php", estoque=json.dumps(estoque, ensure_ascii=False)) or {}

    def deposit_names(self, product_id: str) -> dict[str, str]:
        """Depósitos do produto: nome sem espaços nas pontas -> nome exato (o Olist pode guardar com espaço no fim)."""
        ret = self.call("produto.obter.estoque.php", id=product_id) or {}
        deps = ((ret.get("produto") or {}).get("depositos")) or []
        return {d["deposito"]["nome"].strip(): d["deposito"]["nome"] for d in deps if d.get("deposito")}

    def move_stock(self, product_id: str, quantity: int, kind: str, deposit: str, note: str) -> dict[str, Any]:
        """Entrada (E) ou saída (S) no depósito informado pelo nome exato."""
        estoque = {"estoque": {"idProduto": product_id, "tipo": kind, "quantidade": quantity,
                               "deposito": deposit, "observacoes": note}}
        return self.call("produto.atualizar.estoque.php", estoque=json.dumps(estoque, ensure_ascii=False)) or {}

    # ---------- pedidos ----------

    def find_order_by_ecommerce_number(self, numero_ecommerce: str) -> dict[str, Any] | None:
        ret = self.call("pedidos.pesquisa.php", numeroEcommerce=numero_ecommerce)
        pedidos = (ret or {}).get("pedidos") or []
        return pedidos[0]["pedido"] if pedidos else None

    def search_orders(self, **filters: Any) -> Iterator[dict[str, Any]]:
        """Percorre todas as páginas de pedidos.pesquisa (100 por página)."""
        pagina = 1
        while True:
            ret = self.call("pedidos.pesquisa.php", pagina=pagina, **filters)
            if not ret:
                return
            for p in ret.get("pedidos") or []:
                yield p["pedido"]
            if pagina >= (_int(ret.get("numero_paginas")) or 1):
                return
            pagina += 1

    def find_order_by_number(self, numero: str) -> dict[str, Any] | None:
        """Pedido pelo número interno do Olist (ex.: 10001)."""
        ret = self.call("pedidos.pesquisa.php", numero=numero)
        pedidos = (ret or {}).get("pedidos") or []
        return pedidos[0]["pedido"] if pedidos else None

    def find_sale_invoices_by_number(self, numero: str) -> list[dict[str, Any]]:
        """Notas de saída (venda) com esse número; pode haver mais de uma em séries diferentes."""
        ret = self.call("notas.fiscais.pesquisa.php", numero=numero, tipoNota="S")
        return [n["nota_fiscal"] for n in (ret or {}).get("notas_fiscais") or []]

    def get_order(self, order_id: str) -> dict[str, Any]:
        ret = self.call("pedido.obter.php", id=order_id)
        if not ret:
            raise OlistError(f"Pedido {order_id} não encontrado no Olist")
        return ret["pedido"]

    # ---------- notas fiscais ----------

    def get_invoice(self, invoice_id: str) -> dict[str, Any]:
        ret = self.call("nota.fiscal.obter.php", id=invoice_id)
        if not ret:
            raise OlistError(f"Nota {invoice_id} não encontrada no Olist")
        return ret["nota_fiscal"]

    def find_entry_invoices(self, numero_ecommerce: str) -> list[dict[str, Any]]:
        """Notas de entrada (devolução/retorno) já emitidas para o pedido do marketplace."""
        ret = self.call("notas.fiscais.pesquisa.php", tipoNota="E", numeroEcommerce=numero_ecommerce)
        return [n["nota_fiscal"] for n in (ret or {}).get("notas_fiscais") or []]

    def find_entry_invoices_by_document(self, cpf_cnpj: str) -> list[dict[str, Any]]:
        """Notas de entrada do cliente: a emissão em massa no Olist pode não gravar o nº do pedido."""
        notas, pagina = [], 1
        while True:
            ret = self.call("notas.fiscais.pesquisa.php", tipoNota="E", cpf_cnpj=cpf_cnpj, pagina=pagina)
            notas += [n["nota_fiscal"] for n in (ret or {}).get("notas_fiscais") or []]
            if not ret or pagina >= (_int(ret.get("numero_paginas")) or 1):
                return notas
            pagina += 1

    def invoice_references(self, invoice_id: str, sale_key: str) -> bool:
        """A nota referencia esta NF de venda? Só a autorizada tem XML, e este endpoint não responde em JSON.

        Resposta sem XML de nota (limite de requisições, erro) levanta erro: tratá-la como "não referencia" levaria a
        emitir uma nota duplicada.
        """
        for attempt in range(1, self.max_retries + 1):
            resp = self.http.post(
                f"{self.base_url}/nota.fiscal.obter.xml.php", data={"token": self.token, "id": str(invoice_id)}
            )
            xml = html.unescape(resp.text)
            if "<infNFe" in xml or "<NFe" in xml:
                return f"<chaveAcesso>{sale_key}<" in xml or f"<refNFe>{sale_key}<" in xml
            if attempt < self.max_retries:
                time.sleep(10 * attempt)
        raise OlistError(f"XML da nota {invoice_id} indisponível; não dá para conferir a referência à venda")

    def create_invoice(self, nota_fiscal: dict[str, Any]) -> dict[str, Any]:
        ret = self.call(
            "nota.fiscal.incluir.php",
            nota=json.dumps({"nota_fiscal": nota_fiscal}, ensure_ascii=False),
        )
        # O Olist devolve `registros` como lista [{"registro": {...}}] ou como objeto {"registro": {...}}
        registros = (ret or {}).get("registros") or []
        if isinstance(registros, dict):
            registros = [registros]
        registro = registros[0].get("registro", registros[0]) if registros else {}
        if registro.get("status") != "OK":
            erros = registro.get("erros") or ret
            raise OlistError(f"Olist recusou a nota: {erros}")
        return registro

    def emit_invoice(self, invoice_id: str, send_email: bool = False) -> dict[str, Any]:
        return self.call("nota.fiscal.emitir.php", id=invoice_id, enviar_email="S" if send_email else "N") or {}

    def post_invoice_stock(self, invoice_id: str, deposit_id: int | None = None) -> dict[str, Any]:
        """Lança o estoque da nota. Em nota de entrada (devolução), soma ao estoque."""
        return self.call("nota.fiscal.lancar.estoque.php", id=invoice_id, id_deposito=deposit_id) or {}


def _int(value: Any) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def is_undelivered(situacao: Any) -> bool:
    """Situação 'Não entregue' do pedido (texto na API v2, código 9 na v3)."""
    text = unicodedata.normalize("NFKD", str(situacao)).encode("ascii", "ignore").decode().lower()
    return text.replace("_", " ").strip() in {"nao entregue", "9"}


def unwrap_items(obj: dict[str, Any]) -> list[dict[str, Any]]:
    """O Olist devolve itens como [{"item": {...}}]."""
    return [i.get("item", i) for i in obj.get("itens") or []]


# Situações de nota que indicam autorização na SEFAZ.
# Na API v2 a situação da nota vem como código (6 = Autorizada, 7 = Emitida DANFE)
# ou texto, dependendo do endpoint, por isso aceitamos os dois.
_AUTHORIZED = {"6", "7", "autorizada", "autorizado", "emitida danfe"}
_CANCELLED = {"3", "cancelada", "cancelado", "5", "rejeitada", "10", "denegada"}


def invoice_state(nota: dict[str, Any]) -> str:
    situacao = str(nota.get("situacao", "")).strip().lower()
    if situacao in _AUTHORIZED:
        return "authorized"
    if situacao in _CANCELLED:
        return "cancelled"
    return "draft"
