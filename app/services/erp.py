"""Fluxo da devolução no Olist ERP.

1. Vínculo: acha o pedido no Olist pelo nº do marketplace e a NF de venda.
2. Conferência: ao receber o pacote, a equipe confirma a condição (revenda ou defeito).
3. NF de devolução: entrada referenciando a NF de venda, só com os itens devolvidos.
4. Estoque: com a NF autorizada, lança estoque apenas se o item for revendável.
"""

import logging
import re
import time
from collections import defaultdict
from datetime import UTC, date, datetime, timedelta
from decimal import ROUND_DOWN, Decimal
from functools import lru_cache
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..enums import EventType, ItemCondition, Platform, ReturnKind, ReturnStatus, ReturnType
from ..erp.nfe_xml import find_sale_xml
from ..erp.olist import OlistClient, OlistError, invoice_state, is_undelivered, unwrap_items
from ..models import Return, ReturnEvent, ReturnItem, ReturnReasonRule, Store
from .costs import compute_costs

log = logging.getLogger(__name__)

INVOICE_NOT_FOUND = 32  # código de erro do Olist para "Nota Fiscal não localizada"

CLIENT_FIELDS = (
    "nome", "tipo_pessoa", "cpf_cnpj", "ie", "rg", "endereco", "numero", "complemento",
    "bairro", "cep", "cidade", "uf", "fone", "email",
)
ITEM_OPTIONAL_FIELDS = ("id_produto", "ncm", "origem", "gtin_ean", "cest")

# CFOP da venda (3 últimos dígitos) -> CFOP de devolução sem o 1º dígito. O Olist completa com 1 (mesmo
# estado) ou 2 (outro estado) pela natureza "?.xxx". A API do Olist não aceita CFOP na nota: por isso o
# app escolhe a natureza certa para cada caso (OLIST_RETURN_NATUREZAS).
RETURN_CFOP_FAMILY = {
    "102": "202", "108": "202",  # mercadoria de terceiros (5102 / 6102 / 6108) -> 1202 / 2202
    "101": "201", "107": "201",  # produção própria (5101 / 6101 / 6107) -> 1201 / 2201
    "405": "411", "404": "411",  # terceiros com substituição tributária (5405 / 6404) -> 1411 / 2411
}
SIMPLES_REGIMES = {"1"}  # NF de venda emitida no Simples: devolução espelha sem destacar impostos


class ErpError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.status_code = status_code
        super().__init__(message)


@lru_cache
def get_olist_client() -> OlistClient:
    s = get_settings()
    return OlistClient(s.olist_api_token, s.olist_api_base)


def _event(ret: Return, event_type: EventType, message: str | None = None, actor: str | None = None, **payload):
    ret.events.append(
        ReturnEvent(event_type=event_type, source="erp", actor=actor, message=message, payload=payload or None)
    )


def _note_error(db: Session, ret: Return, exc: Exception) -> None:
    """Guarda o erro de uma nova tentativa só se mudou (depois do rollback, gravar direto sempre gerava um UPDATE)."""
    if ret.erp_last_error != str(exc):
        ret.erp_last_error = str(exc)
        db.commit()


def _record_error(db: Session, ret: Return, exc: Exception) -> None:
    ret.erp_last_error = str(exc)
    _event(ret, EventType.ERP_ERROR, str(exc))
    db.commit()


# ---------- classificação ----------

Rule = tuple[str, str, int]


def load_rules(db: Session) -> list[Rule]:
    rows = db.scalars(select(ReturnReasonRule)).all()
    return [(r.keyword, r.condition, r.priority) for r in rows]


def classify(reason_code: str | None, reason_text: str | None, rules: list[Rule]) -> str | None:
    """Sugere a condição a partir do motivo do comprador. Regras com menor prioridade vencem."""
    text = f"{reason_code or ''} {reason_text or ''}".lower().replace("_", " ")
    for keyword, condition, _ in sorted(rules, key=lambda r: r[2]):
        if re.search(rf"\b{re.escape(keyword.lower())}\b", text):
            return condition
    return None


def buyer_comment(ret: Return) -> str | None:
    """Comentário livre que o comprador escreveu ao pedir a devolução (Shopee: text_reason)."""
    raw = ret.raw or {}
    text = (raw.get("text_reason") or "").strip()
    return text if text and text != (ret.reason_text or "") else None


def apply_suggested_condition(ret: Return, rules: list[Rule]) -> None:
    """Atualiza a sugestão automática, sem sobrescrever o que a equipe definiu na conferência."""
    if ret.condition_source == "manual" or ret.kind == ReturnKind.UNDELIVERED:
        return
    # O comentário do comprador ajuda: "não serviu" + "veio com mancha" sugere defeito
    text = " ".join(t for t in (ret.reason_text, buyer_comment(ret)) if t)
    condition = classify(ret.reason_code, text, rules)
    ret.item_condition = condition
    ret.condition_source = "auto" if condition else None


# ---------- vínculo com o pedido ----------

def _ecommerce_numbers(ret: Return) -> list[str]:
    """Números que o Olist pode ter gravado como nº do pedido no marketplace.

    No Mercado Livre, pedidos de um mesmo carrinho formam um pacote (pack_id), e a integração pode usá-lo.
    """
    numbers = [ret.external_order_id]
    pack_id = ((ret.raw or {}).get("order") or {}).get("pack_id")
    if pack_id and str(pack_id) != ret.external_order_id:
        numbers.append(str(pack_id))
    return [n for n in numbers if n]


def link_return(db: Session, ret: Return, client: OlistClient | None = None) -> None:
    client = client or get_olist_client()
    if not ret.external_order_id:
        raise ErpError("Devolução sem número de pedido do marketplace", 422)

    summary = None
    for number in _ecommerce_numbers(ret):
        if summary := client.find_order_by_ecommerce_number(number):
            break
    if not summary:
        raise ErpError(f"Pedido {ret.external_order_id} não encontrado no Olist", 404)
    order = client.get_order(str(summary["id"]))

    ret.erp_order_id = str(order["id"])
    ret.erp_order_number = str(order.get("numero") or "")
    # Rastreio da ida: o cliente costuma devolver no mesmo pacote, com a etiqueta do envio
    ret.sale_tracking_number = (order.get("codigo_rastreamento") or "").strip().upper() or None
    invoice_id = str(order.get("id_nota_fiscal") or "")
    if invoice_id and invoice_id != "0":
        ret.erp_sale_invoice_id = invoice_id
        ret.erp_sale_invoice_key = client.get_invoice(invoice_id).get("chave_acesso") or None
    ret.erp_last_error = None
    _event(
        ret,
        EventType.ERP_LINKED,
        f"Pedido Olist nº {ret.erp_order_number}",
        erp_order_id=ret.erp_order_id,
        erp_sale_invoice_id=ret.erp_sale_invoice_id,
    )
    db.commit()


def link_pending(db: Session) -> None:
    """Job: vincula devoluções recentes que ainda não têm pedido do Olist."""
    s = get_settings()
    pending = db.scalars(
        select(Return)
        .where(
            Return.erp_order_id.is_(None),
            Return.external_order_id.is_not(None),
            Return.status != ReturnStatus.CANCELLED,
            Return.requested_at >= datetime.now(UTC) - timedelta(days=60),
        )
        .order_by(Return.requested_at.desc())
        .limit(s.olist_link_batch_size)
    ).all()
    for ret in pending:
        try:
            link_return(db, ret)
        except (ErpError, OlistError) as exc:
            db.rollback()
            _note_error(db, ret, exc)  # sem evento para não poluir o histórico a cada tentativa


# ---------- NF de devolução ----------

def build_return_invoice(ret: Return, sale_invoice: dict[str, Any], settings: Settings, today: date) -> dict[str, Any]:
    """Monta a NF de entrada (devolução) com os itens devolvidos, copiando dados fiscais da NF de venda."""
    sold = {str(i.get("codigo")).strip(): i for i in unwrap_items(sale_invoice)}

    wanted: dict[str, Decimal] = defaultdict(Decimal)
    marketplace_value: dict[str, Decimal] = defaultdict(Decimal)
    for item in ret.items:
        sku = str(item.seller_sku).strip()  # a Shopee pode mandar o SKU com tabulação na frente
        wanted[sku] += Decimal(item.quantity or 1)
        if (v := _item_product_value(item)) is not None:
            marketplace_value[sku] += v
    if not wanted:  # sem detalhe de itens: devolução do pedido inteiro
        wanted = {sku: Decimal(str(i.get("quantidade") or 1)) for sku, i in sold.items()}

    missing = [sku for sku in wanted if sku not in sold]
    if missing:
        raise ErpError(
            f"SKU(s) {', '.join(missing)} não constam na NF de venda. Confira se o SKU do marketplace "
            "é igual ao código do produto no Olist.",
            422,
        )

    # Desconto dado no total da NF de venda é rateado proporcionalmente entre os itens:
    # a devolução precisa espelhar o valor efetivamente faturado de cada item.
    produtos = _decimal(sale_invoice.get("valor_produtos")) or sum(
        (_decimal(i.get("valor_unitario")) or Decimal(0)) * Decimal(str(i.get("quantidade") or 0)) for i in sold.values()
    )
    desconto = _decimal(sale_invoice.get("valor_desconto")) or Decimal(0)
    fator = (produtos - desconto) / produtos if produtos and desconto > 0 else Decimal(1)

    units: dict[str, Decimal] = {}
    for sku, qty in wanted.items():
        s = sold[sku]
        if qty > Decimal(str(s.get("quantidade") or 0)):
            raise ErpError(f"Quantidade devolvida do SKU {sku} maior que a vendida", 422)
        gross_unit = _decimal(s.get("valor_unitario")) or Decimal(0)
        unit = gross_unit * fator
        if settings.olist_return_value_source == "marketplace" and marketplace_value.get(sku):
            # Valor real da peça no marketplace (o Olist pode ter rateado o desconto do pedido de forma
            # diferente); nunca acima do preço cheio da peça na NF de venda.
            unit = min(marketplace_value[sku] / qty, gross_unit)
        units[sku] = unit
    # A devolução nunca passa do valor líquido da venda (desconto no total da nota)
    net_sale = produtos - desconto
    total = sum(units[sku] * qty for sku, qty in wanted.items())
    if desconto > 0 and total > net_sale > 0:
        scale = net_sale / total
        units = {sku: (u * scale).quantize(Decimal("0.01"), rounding=ROUND_DOWN) for sku, u in units.items()}

    itens = []
    for sku, qty in wanted.items():
        s = sold[sku]
        item = {
            "codigo": sku,
            "descricao": s.get("descricao"),
            "unidade": s.get("unidade") or "UN",
            "quantidade": str(qty),
            "valor_unitario": str(units[sku].quantize(Decimal("0.01"))),
            "tipo": "P",
        }
        item.update({k: s[k] for k in ITEM_OPTIONAL_FIELDS if s.get(k)})
        itens.append({"item": item})

    undelivered = ret.kind == ReturnKind.UNDELIVERED
    cliente_venda = sale_invoice.get("cliente") or {}
    nota: dict[str, Any] = {
        "tipo": "E",
        "natureza_operacao": (
            settings.olist_natureza_undelivered if undelivered else settings.olist_natureza_operacao
        ),
        "data_emissao": today.strftime("%d/%m/%Y"),
        "finalidade": settings.olist_undelivered_finalidade if undelivered else settings.olist_invoice_finalidade,
        "refNFe": ret.erp_sale_invoice_key,
        "cliente": {k: cliente_venda[k] for k in CLIENT_FIELDS if cliente_venda.get(k)},
        "itens": itens,
        "frete_por_conta": settings.olist_frete_por_conta,
        "numero_pedido_ecommerce": ret.external_order_id,
        "obs": (
            (
                f"Retorno nao entregue pedido {ret.external_order_id}"
                if undelivered
                else f"Devolucao {ret.platform} {ret.external_return_id}"
            )
            + (f" ref NF {sale_invoice['numero']}" if sale_invoice.get("numero") else "")
        )[:100],
    }
    by_cfop = parse_return_naturezas(settings.olist_return_naturezas)
    # Não entregue: o contador orientou o mesmo CFOP da devolução (1.202/2.202), salvo natureza própria configurada
    if by_cfop and not (undelivered and settings.olist_natureza_undelivered_id):
        key = return_natureza_key([sold[sku] for sku in wanted], sale_invoice)
        if key not in by_cfop:
            raise ErpError(
                f"Falta configurar a natureza de devolução para CFOP x.{key[:3]}"
                + (" de venda feita no Simples" if key.endswith("S") else "")
                + " (OLIST_RETURN_NATUREZAS)",
                422,
            )
        nota["id_natureza_operacao"] = by_cfop[key]
        del nota["natureza_operacao"]  # vale o id; o nome poderia apontar para outra natureza
        return nota
    if undelivered:
        natureza_id = settings.olist_natureza_undelivered_id
    elif ret.item_condition == ItemCondition.RESALE:
        natureza_id = settings.olist_natureza_resale_id
    else:
        # Defeito e item diferente do pedido: NF emitida normalmente, natureza que não lança estoque
        natureza_id = settings.olist_natureza_defect_id
    if natureza_id:
        nota["id_natureza_operacao"] = natureza_id
    return nota


def parse_return_naturezas(raw: str) -> dict[str, int]:
    """'202:123, 202S:456' -> {'202': 123, '202S': 456}."""
    out: dict[str, int] = {}
    for part in (raw or "").split(","):
        if ":" in part:
            key, value = (p.strip() for p in part.split(":", 1))
            if key and value.isdigit():
                out[key.upper()] = int(value)
    return out


def return_natureza_key(sale_items: list[dict[str, Any]], sale_invoice: dict[str, Any]) -> str:
    """Qual natureza de devolução usar, a partir dos CFOPs dos itens vendidos e do regime da NF de venda."""
    families = set()
    for item in sale_items:
        cfop = re.sub(r"\D", "", str(item.get("cfop") or ""))
        family = RETURN_CFOP_FAMILY.get(cfop[-3:]) if len(cfop) == 4 else None
        if family is None:
            raise ErpError(
                f"CFOP {cfop or '(vazio)'} da NF de venda sem regra de devolução. Fale com a contabilidade "
                "e gere esta NF manualmente no Olist.",
                422,
            )
        families.add(family)
    if len(families) > 1:
        raise ErpError(
            f"Os itens devolvidos têm CFOPs de tipos diferentes na NF de venda ({', '.join(sorted(families))}). "
            "Gere esta NF manualmente no Olist.",
            422,
        )
    simples = str(sale_invoice.get("regime_tributario") or "") in SIMPLES_REGIMES
    return families.pop() + ("S" if simples else "")


def _item_product_value(item: ReturnItem) -> Decimal | None:
    """Valor do produto devolvido segundo o marketplace, sem frete (TikTok: refund_subtotal)."""
    refund = (item.raw or {}).get("refund_amount")
    if isinstance(refund, dict):  # TikTok: {"refund_subtotal": ..., "refund_total": ...}
        return _decimal(refund.get("refund_subtotal")) or item.refund_amount
    return item.refund_amount  # Shopee/Mercado Livre: o valor já vem no item


def load_sale_invoice(client: OlistClient, ret: Return) -> dict[str, Any]:
    """NF de venda do Olist; se ela foi apagada de lá, monta a partir do XML guardado (OLIST_SALE_XML_ZIPS)."""
    if ret.erp_sale_invoice_id:
        try:
            return client.get_invoice(ret.erp_sale_invoice_id)
        except OlistError as exc:
            if exc.code != INVOICE_NOT_FOUND:
                raise
    sale = find_sale_xml(get_settings().olist_sale_xml_zips, ret.erp_sale_invoice_key)
    if not sale:
        raise ErpError(
            "A NF de venda não existe mais no Olist e o XML dela não está nos arquivos guardados "
            f"(chave {ret.erp_sale_invoice_key or 'desconhecida'})",
            409,
        )
    for entry in sale["itens"]:
        item = entry["item"]
        try:  # liga o item ao produto do Olist para o estoque; produto excluído fica sem vínculo
            item["id_produto"] = client.find_product_id_by_sku(item["codigo"]) or ""
        except OlistError:
            item["id_produto"] = ""
    return sale


def create_return_invoice(db: Session, ret: Return, actor: str | None = None) -> None:
    s = get_settings()
    client = get_olist_client()
    if s.olist_invoice_mode == "off":
        raise ErpError("Criação de NF desativada (OLIST_INVOICE_MODE=off)", 409)
    if ret.erp_return_invoice_id and ret.erp_return_invoice_status != "cancelled":
        raise ErpError(f"NF de devolução já criada (id {ret.erp_return_invoice_id})", 409)
    if not ret.item_condition:
        raise ErpError("Defina a condição do item (resale/defect) antes de gerar a NF", 409)
    # Proteção contra duplicidade: a equipe pode já ter emitido a NF de devolução à mão no Olist
    if ret.external_order_id and _link_existing_entry_invoice(db, ret, client, actor):
        return

    if not ret.erp_sale_invoice_key:
        link_return(db, ret, client)
    if not ret.erp_sale_invoice_id or not ret.erp_sale_invoice_key:
        raise ErpError("Pedido sem NF de venda autorizada no Olist", 409)

    sale = load_sale_invoice(client, ret)
    if _link_entry_invoice_of_sale(db, ret, client, sale, actor):
        return
    nota = build_return_invoice(ret, sale, s, datetime.now(ZoneInfo(s.timezone)).date())
    registro = client.create_invoice(nota)

    ret.erp_return_invoice_id = str(registro["id"])
    ret.erp_return_invoice_number = str(registro.get("numero") or "")
    ret.erp_return_invoice_status = "draft"
    ret.erp_stock_status = None
    ret.erp_last_error = None
    _event(
        ret,
        EventType.ERP_INVOICE_CREATED,
        f"NF de devolução nº {ret.erp_return_invoice_number} criada no Olist",
        actor=actor,
        erp_invoice_id=ret.erp_return_invoice_id,
    )
    db.commit()

    if s.olist_invoice_mode == "emit":
        emit_return_invoice(db, ret)


def _link_existing_entry_invoice(db: Session, ret: Return, client: OlistClient, actor: str | None) -> bool:
    """Se já existe NF de entrada (não cancelada) para o pedido, vincula e não cria outra.

    Procura pelo nº do pedido e, no Mercado Livre, também pelo nº do pacote: a emissão feita no Olist (inclusive
    a emissão em massa) grava o número que a integração usou, que no ML costuma ser o do pacote.
    """
    found: dict[str, dict] = {}
    for number in _ecommerce_numbers(ret):
        for n in client.find_entry_invoices(number):
            if invoice_state(n) != "cancelled":
                found[str(n.get("id"))] = n
    existing = list(found.values())
    skus = {str(i.seller_sku).strip() for i in ret.items if i.seller_sku}
    if existing and skus:
        # Um pedido pode ter duas devoluções de peças diferentes: a nota de uma não serve para a outra
        existing = [n for n in existing if skus & {
            str(i.get("codigo")).strip() for i in unwrap_items(client.get_invoice(str(n.get("id"))))}]
    if not existing:
        return False
    _adopt_entry_invoice(db, ret, existing, actor)
    if len(existing) > 1:
        numeros = ", ".join(str(n.get("numero")) for n in existing)
        log.warning("Pedido %s tem %d NFs de entrada no Olist: %s", ret.external_order_id, len(existing), numeros)
    return True


def _adopt_entry_invoice(db: Session, ret: Return, existing: list[dict[str, Any]], actor: str | None) -> None:
    nota = existing[0]
    ret.erp_return_invoice_id = str(nota.get("id"))
    ret.erp_return_invoice_number = str(nota.get("numero") or "")
    ret.erp_return_invoice_status = invoice_state(nota)
    # As notas emitidas fora do app (inclusive em massa) não lançam estoque: ninguém sabia quais peças estavam boas.
    # O estoque sai pela conferência, como nas notas do app, quando a nota estiver autorizada.
    ret.erp_stock_status = None
    ret.erp_last_error = None
    numeros = ", ".join(str(n.get("numero")) for n in existing)
    _event(
        ret,
        EventType.ERP_INVOICE_FOUND,
        f"NF de devolução já existente no Olist ({numeros}); nenhuma nota nova foi criada. "
        "O estoque será lançado pela conferência",
        actor=actor,
        erp_invoice_ids=[str(n.get("id")) for n in existing],
    )
    db.commit()


def _link_entry_invoice_of_sale(
    db: Session, ret: Return, client: OlistClient, sale: dict[str, Any], actor: str | None
) -> bool:
    """A emissão em massa no Olist pode não gravar o nº do pedido: procura pelas notas de entrada do cliente.

    A autorizada vale se referenciar esta NF de venda. A que ainda não tem XML vale pelo mesmo SKU: melhor parar numa
    nota parecida do que emitir duas para a mesma peça. Autorizada de outra venda com a mesma peça pode ser outra
    compra ou um erro na nota: para e pede conferência em vez de emitir.
    """
    document = re.sub(r"\D", "", str((sale.get("cliente") or {}).get("cpf_cnpj") or ""))
    if not document:
        # Sem CPF/CNPJ não dá para achar nota emitida em massa sem o nº do pedido: melhor parar do que duplicar
        raise ErpError("NF de venda sem CPF/CNPJ do cliente: confira no Olist se já existe NF de devolução", 409)
    skus = {str(i.seller_sku).strip() for i in ret.items}
    suspicious: list[str] = []
    for nota in client.find_entry_invoices_by_document(document):
        state = invoice_state(nota)
        if state == "cancelled":
            continue
        detail = client.get_invoice(str(nota.get("id")))
        # Só a nota com a peça desta devolução: um pedido com duas peças pode ter uma nota para cada uma
        if not skus & {str(i.get("codigo")).strip() for i in unwrap_items(detail)}:
            continue
        if state != "authorized" or client.invoice_references(str(nota.get("id")), ret.erp_sale_invoice_key):
            _adopt_entry_invoice(db, ret, [nota], actor)
            return True
        suspicious.append(str(nota.get("numero")))
    if suspicious:
        raise ErpError(
            f"O cliente já tem NF de devolução com a mesma peça para outra venda ({', '.join(suspicious)}). "
            "Confira no Olist antes de emitir",
            409,
        )
    return False


def emit_return_invoice(db: Session, ret: Return) -> None:
    if not ret.erp_return_invoice_id:
        raise ErpError("Nenhuma NF de devolução criada", 409)
    get_olist_client().emit_invoice(ret.erp_return_invoice_id)
    refresh_invoice_status(db, ret)


def refresh_invoice_status(db: Session, ret: Return) -> None:
    if not ret.erp_return_invoice_id:
        return
    try:
        nota = get_olist_client().get_invoice(ret.erp_return_invoice_id)
    except OlistError as exc:
        if exc.code != INVOICE_NOT_FOUND:
            raise
        # Rascunho excluído no Olist: libera para gerar uma nova NF
        nota = {"situacao": "cancelada"}
    state = invoice_state(nota)
    if state == ret.erp_return_invoice_status:
        return
    ret.erp_return_invoice_status = state
    if state == "authorized":
        _event(ret, EventType.ERP_INVOICE_AUTHORIZED, f"NF {ret.erp_return_invoice_number} autorizada")
    db.commit()
    if state == "authorized":
        post_stock(db, ret)


# ---------- estoque ----------

def post_stock(db: Session, ret: Return) -> None:
    """Revenda: lança estoque. Defeito e item diferente do pedido: vão para o depósito de defeitos (que não conta
    como estoque disponível), se configurado; sem ele, não lançam.

    O "lançar estoque" da nota no Olist põe a entrada sempre no depósito padrão (o de venda). Para a peça nunca
    aparecer para venda, o app primeiro tira a quantidade do padrão, depois lança a nota (que só repõe o que saiu) e
    por fim dá entrada no depósito certo. Cada etapa fica gravada para a próxima rodada continuar de onde parou.
    """
    s = get_settings()
    if ret.erp_stock_status in (STOCK_RESERVED, STOCK_LAUNCHED):
        _finish_stock(db, ret)
        return
    if ret.erp_stock_status:
        return
    if ret.erp_return_invoice_status != "authorized":
        raise ErpError("A NF de devolução ainda não foi autorizada", 409)
    if _stock_of_invoice_handled(db, ret):
        ret.erp_stock_status = "external"
        _event(ret, EventType.ERP_STOCK_SKIPPED,
               f"Estoque da NF {ret.erp_return_invoice_number} já lançado por outra devolução da mesma nota")
        ret.erp_last_error = None
        db.commit()
        return

    if ret.item_condition == ItemCondition.RESALE:
        if not s.olist_post_stock:
            _stock_done(db, ret, "Estoque lançado pela natureza de operação do Olist")
            return
    elif ret.item_condition in (ItemCondition.DEFECT, ItemCondition.MISMATCH):
        if not (s.olist_post_stock and s.olist_defect_deposit_id):
            ret.erp_stock_status = f"skipped_{ret.item_condition}"
            _event(ret, EventType.ERP_STOCK_SKIPPED, "Sem depósito de defeitos configurado: não retorna ao estoque")
            ret.erp_last_error = None
            db.commit()
            return
    else:
        raise ErpError("Condição do item não definida", 409)

    if _uses_transfer(ret):
        moves, missing = _stock_moves(ret)  # confere produtos e depósitos antes de mexer em qualquer saldo
        if not moves:
            ret.erp_stock_status = STOCK_NO_PRODUCT
            _event(ret, EventType.ERP_STOCK_SKIPPED,
                   f"Produto excluído no cadastro do Olist ({', '.join(missing)}): estoque não lançado")
            ret.erp_last_error = None
            db.commit()
            return
        client = get_olist_client()
        for product_id, qty, source, _dest, note, linked in moves:
            if linked:  # item sem produto na nota: o lançamento da nota não mexe no saldo, então não tira nada
                client.move_stock(product_id, qty, "S", source, note)
        ret.erp_stock_status = STOCK_RESERVED
        ret.erp_last_error = None
        db.commit()
    _finish_stock(db, ret)


def _stock_of_invoice_handled(db: Session, ret: Return) -> bool:
    """Outra devolução com a mesma NF já lançou (ou está lançando) o estoque dela: lançar de novo duplicaria."""
    return db.scalar(
        select(func.count()).select_from(Return).where(
            Return.erp_return_invoice_id == ret.erp_return_invoice_id,
            Return.id != ret.id,
            Return.erp_stock_status.in_(("posted", STOCK_RESERVED, STOCK_LAUNCHED)),
        )
    ) > 0


STOCK_RESERVED = "reserved"  # já saiu do depósito padrão; falta lançar a nota
STOCK_LAUNCHED = "launched"  # nota lançada; falta a entrada no depósito certo
STOCK_NO_PRODUCT = "skipped_no_product"  # produto excluído no cadastro do Olist


def _uses_transfer(ret: Return) -> bool:
    s = get_settings()
    return bool(_target_deposit_name(ret).strip() and s.olist_default_deposit_name.strip())


def _target_deposit_name(ret: Return) -> str:
    s = get_settings()
    return s.olist_resale_deposit_name if ret.item_condition == ItemCondition.RESALE else s.olist_defect_deposit_name


def _where(ret: Return) -> str:
    if ret.item_condition == ItemCondition.RESALE:
        return "depósito de devoluções (aguardando transferência para o MS)"
    return "depósito de defeitos"


def _stock_moves(ret: Return) -> tuple[list[tuple[str, int, str, str, str, bool]], list[str]]:
    """Movimentos de cada item da nota e os SKUs sem produto no Olist.

    Movimento: (produto, quantidade, depósito padrão, depósito certo, observação, vinculado à nota). Item com
    id_produto 0 não está vinculado: o lançamento da nota não mexe no saldo dele.
    """
    s = get_settings()
    client = get_olist_client()
    target = _target_deposit_name(ret).strip()
    note = f"NF de devolução {ret.erp_return_invoice_number}: entrada no {_where(ret)} (app de devoluções)"
    moves, missing = [], []
    for item in unwrap_items(client.get_invoice(ret.erp_return_invoice_id)):
        sku = str(item.get("codigo")).strip()
        raw_id = str(item.get("id_produto") or "").strip()
        linked = raw_id not in ("", "0")
        product_id = raw_id if linked else client.find_product_id_by_sku(sku)
        if not product_id:
            missing.append(sku)
            continue
        names = client.deposit_names(product_id)
        source, dest = names.get(s.olist_default_deposit_name.strip()), names.get(target)
        if not source or not dest:
            raise ErpError(f"Depósito não encontrado no produto {sku}: {target}", 409)
        moves.append((product_id, int(Decimal(str(item.get("quantidade") or 1))), source, dest, note, linked))
    return moves, missing


def _finish_stock(db: Session, ret: Return) -> None:
    s = get_settings()
    client = get_olist_client()
    if ret.erp_stock_status in (None, STOCK_RESERVED):
        deposit_id = s.olist_resale_deposit_id if ret.item_condition == ItemCondition.RESALE else s.olist_defect_deposit_id
        client.post_invoice_stock(ret.erp_return_invoice_id, deposit_id)
        if not _uses_transfer(ret):
            _stock_done(db, ret, "Itens lançados no depósito de defeitos"
                        if ret.item_condition != ItemCondition.RESALE else "Itens devolvidos ao estoque")
            return
        # Grava logo: lançar de novo a mesma nota duplicaria a entrada
        ret.erp_stock_status = STOCK_LAUNCHED
        ret.erp_last_error = None
        db.commit()
    moves, _missing = _stock_moves(ret)
    for product_id, qty, _source, dest, note, _linked in moves:
        client.move_stock(product_id, qty, "E", dest, note)
    _stock_done(db, ret, f"Itens lançados no {_where(ret)}")


def _stock_done(db: Session, ret: Return, message: str) -> None:
    ret.erp_stock_status = "posted"
    ret.erp_stock_posted_at = datetime.now(UTC)
    ret.erp_last_error = None
    _event(ret, EventType.ERP_STOCK_POSTED, message)
    db.commit()


# ---------- retorno de mercadoria não entregue ----------

def _decimal(value: Any) -> Decimal | None:
    try:
        return Decimal(str(value)) if value not in (None, "") else None
    except ArithmeticError:
        return None


def create_undelivered_from_order(
    db: Session, store: Store, order: dict[str, Any], source: str, actor: str | None = None, notes: str | None = None
) -> Return:
    """Registra o retorno a partir do pedido completo do Olist (pedido.obter)."""
    ecommerce = order.get("ecommerce") or {}
    order_number_mkt = ecommerce.get("numeroPedidoEcommerce") or order.get("numero_ecommerce")
    ret = Return(
        store_id=store.id,
        platform=store.platform,
        kind=ReturnKind.UNDELIVERED,
        external_return_id=f"olist-{order['id']}",
        external_order_id=str(order_number_mkt) if order_number_mkt else None,
        return_type=ReturnType.UNKNOWN,
        status=ReturnStatus.RETURNING_TO_SELLER,
        platform_status=str(order.get("situacao") or ""),
        reason_text="Mercadoria não entregue ao destinatário",
        refund_amount=_decimal(order.get("total_pedido")),
        currency="BRL",
        requested_at=datetime.now(UTC),
        item_condition=ItemCondition.RESALE,  # pacote não foi aberto; a conferência confirma
        condition_source="auto",
        erp_order_id=str(order["id"]),
        erp_order_number=str(order.get("numero") or ""),
        internal_notes=notes,
        raw={"olist_order": order},
    )
    ret.items = [
        ReturnItem(
            seller_sku=i.get("codigo"),
            product_name=i.get("descricao"),
            quantity=int(Decimal(str(i.get("quantidade") or 1))),
            refund_amount=(_decimal(i.get("valor_unitario")) or Decimal(0)) * Decimal(str(i.get("quantidade") or 1)),
            raw=i,
        )
        for i in unwrap_items(order)
    ]
    compute_costs(ret)
    # A etiqueta que volta no pacote é a do envio da venda: sem ela no app, a busca pela etiqueta não acha o retorno
    ret.sale_tracking_number = (order.get("codigo_rastreamento") or "").strip().upper() or _marketplace_tracking(
        db, store, ret.external_order_id
    )
    invoice_id = str(order.get("id_nota_fiscal") or "")
    if invoice_id and invoice_id != "0":
        ret.erp_sale_invoice_id = invoice_id
        ret.erp_sale_invoice_key = get_olist_client().get_invoice(invoice_id).get("chave_acesso") or None
    ret.events.append(
        ReturnEvent(
            event_type=EventType.CREATED,
            source=source,
            actor=actor,
            to_status=ret.status,
            message=f"Pedido Olist nº {ret.erp_order_number} não entregue ao destinatário",
        )
    )
    db.add(ret)
    db.commit()
    return ret


def _marketplace_tracking(db: Session, store: Store, order_id: str | None) -> str | None:
    """Rastreio do envio da venda direto no marketplace, quando o Olist não tem (ex.: Shopee Xpress)."""
    if not order_id or store.platform != Platform.SHOPEE:
        return None
    try:
        from ..platforms.registry import get_shopee_client
        from .tokens import ensure_shopee_token

        return get_shopee_client().get_tracking_number(ensure_shopee_token(db, store), store.external_shop_id, order_id)
    except Exception as exc:  # noqa: BLE001 - sem rastreio o retorno continua sendo registrado
        log.warning("Rastreio da venda %s na Shopee: %s", order_id, exc)
        return None


def _existing_undelivered(db: Session, erp_order_id: str) -> Return | None:
    return db.scalar(
        select(Return).where(Return.kind == ReturnKind.UNDELIVERED, Return.erp_order_id == erp_order_id)
    )


def register_undelivered(
    db: Session, store: Store, external_order_id: str, actor: str | None, notes: str | None
) -> Return:
    """Cadastro manual: pacote voltou e o pedido ainda não está como 'Não entregue' no Olist."""
    client = get_olist_client()
    summary = client.find_order_by_ecommerce_number(external_order_id)
    if not summary:
        raise ErpError(f"Pedido {external_order_id} não encontrado no Olist", 404)
    if existing := _existing_undelivered(db, str(summary["id"])):
        raise ErpError(f"Retorno já registrado (id {existing.id})", 409)
    return create_undelivered_from_order(db, store, client.get_order(str(summary["id"])), "api", actor, notes)


def find_order_for_code(client: OlistClient, code: str) -> dict[str, Any] | None:
    """Acha o pedido do Olist a partir do que está na etiqueta do pacote.

    Aceita a chave da NF (44 dígitos do código de barras do DANFE), o nº do pedido no marketplace
    ou o nº do pedido no Olist. Devolve o pedido completo (pedido.obter) ou None.
    """
    code = re.sub(r"\s+", "", code or "")
    if not code:
        return None
    if re.fullmatch(r"\d{44}", code):
        # Chave de acesso: posições 26-34 são o número da NF
        for nota in client.find_sale_invoices_by_number(str(int(code[25:34]))):
            if (nota.get("chave_acesso") or code) != code:
                continue  # mesma numeração em outra série
            number = nota.get("numero_ecommerce")
            summary = client.find_order_by_ecommerce_number(str(number)) if number else None
            if summary:
                return client.get_order(str(summary["id"]))
            if nota.get("id_venda"):
                return client.get_order(str(nota["id_venda"]))
        return None
    summary = client.find_order_by_ecommerce_number(code)
    if not summary and code.isdigit() and len(code) <= 9:
        summary = client.find_order_by_number(code)
    return client.get_order(str(summary["id"])) if summary else None


def existing_return_for_order(db: Session, order: dict[str, Any]) -> Return | None:
    """Devolução já registrada para esse pedido (do marketplace ou de não entregue)."""
    ecommerce = order.get("ecommerce") or {}
    numbers = {str(n) for n in (ecommerce.get("numeroPedidoEcommerce"), order.get("numero_ecommerce")) if n}
    conds = [Return.erp_order_id == str(order["id"])]
    if numbers:
        conds.append(Return.external_order_id.in_(numbers))
    return db.scalar(select(Return).where(or_(*conds)).order_by(Return.requested_at.desc()).limit(1))


def register_undelivered_by_code(
    db: Session, code: str, actor: str | None, notes: str | None = None
) -> tuple[Return, bool]:
    """Registra o pacote que voltou pela transportadora a partir da etiqueta. Retorna (devolução, criada?).

    Se já houver devolução para o pedido, devolve a existente (não cria outra).
    """
    client = get_olist_client()
    order = find_order_for_code(client, code)
    if not order:
        raise ErpError("Pedido não encontrado no Olist com esse código (chave da NF, pedido do marketplace ou do Olist)", 404)
    if existing := existing_return_for_order(db, order):
        return existing, False
    ecommerce_id = str((order.get("ecommerce") or {}).get("id") or "")
    store = db.scalar(select(Store).where(Store.erp_ecommerce_id == ecommerce_id)) if ecommerce_id else None
    if store is None:
        name = (order.get("ecommerce") or {}).get("nomeEcommerce") or "sem integração"
        raise ErpError(f"O pedido é de um canal sem loja cadastrada no sistema ({name})", 422)
    return create_undelivered_from_order(db, store, order, "api", actor, notes), True


def import_undelivered(db: Session) -> dict[str, Any]:
    """Job: importa pedidos marcados como 'Não entregue' no Olist, de qualquer loja."""
    s = get_settings()
    client = get_olist_client()
    since = datetime.now(ZoneInfo(s.timezone)) - timedelta(days=s.olist_undelivered_lookback_days)
    stores = {st.erp_ecommerce_id: st for st in db.scalars(select(Store).where(Store.erp_ecommerce_id.is_not(None)))}
    result: dict[str, Any] = {"created": 0, "skipped": 0, "unmapped": {}, "errors": []}

    for summary in client.search_orders(
        situacao=s.olist_undelivered_situacao, dataAtualizacao=since.strftime("%d/%m/%Y %H:%M:%S")
    ):
        order_id = str(summary["id"])
        if not is_undelivered(summary.get("situacao", s.olist_undelivered_situacao)) or _existing_undelivered(
            db, order_id
        ):
            result["skipped"] += 1
            continue
        try:
            order = client.get_order(order_id)
            ecommerce = order.get("ecommerce") or {}
            store = stores.get(str(ecommerce.get("id")))
            if store is None:
                result["unmapped"][str(ecommerce.get("id"))] = ecommerce.get("nomeEcommerce")
                continue
            create_undelivered_from_order(db, store, order, source="erp")
            result["created"] += 1
        except (ErpError, OlistError) as exc:
            db.rollback()
            result["errors"].append(f"Pedido Olist {order_id}: {exc}")

    if result["unmapped"]:
        log.warning("Integrações do Olist sem loja vinculada (PATCH /stores/{id}): %s", result["unmapped"])
    return result


# ---------- orquestração ----------

def process_received(db: Session, ret: Return, actor: str | None = None) -> None:
    """Chamado após a conferência. Falhas no ERP ficam registradas sem desfazer o recebimento."""
    if get_settings().olist_invoice_mode == "off" or ret.erp_return_invoice_id:
        return
    try:
        create_return_invoice(db, ret, actor)
    except (ErpError, OlistError) as exc:
        db.rollback()
        _record_error(db, ret, exc)


def create_pending_invoices(db: Session, limit: int = 40) -> dict[str, int]:
    """Job: cria o rascunho da NF de devolução das conferidas sem nota, fora do clique da conferência.

    Assim a operadora não espera o Olist ao confirmar. Só rascunho (modo draft): a autorização na Sefaz é feita
    depois da validação. Quem deu erro (venda apagada, SKU diferente...) fica com o erro e sai da fila; erro de
    limite do Olist volta na próxima rodada.
    """
    if get_settings().olist_invoice_mode == "off":
        return {}
    rows = db.scalars(
        select(Return).where(
            Return.condition_source == "manual",
            Return.item_condition.is_not(None),
            Return.received_at.is_not(None),
            Return.handled_outside_at.is_(None),
            func.coalesce(Return.received_by, "") != "teste-api",
            or_(Return.erp_return_invoice_id.is_(None), Return.erp_return_invoice_status == "cancelled"),
            or_(Return.erp_last_error.is_(None), Return.erp_last_error.like("%erro 6%")),
        ).order_by(Return.received_at).limit(limit)
    ).all()
    result = {"criadas": 0, "erros": 0}
    for ret in rows:
        try:
            create_return_invoice(db, ret, "Rotina (rascunho automático)")
            result["criadas"] += 1
        except (ErpError, OlistError) as exc:
            db.rollback()
            _record_error(db, ret, exc)
            result["erros"] += 1
        time.sleep(2)  # o token do Olist é o mesmo das outras rotinas
    return result


RETRYABLE = ("erro 6", "instável", "instavel", "Tentativas", "conexão")


def check_return_invoice(client: OlistClient, ret: Return) -> list[str]:
    """Confere o rascunho contra a NF de venda antes de autorizar. Lista vazia = pode autorizar."""
    nota = client.get_invoice(ret.erp_return_invoice_id)
    venda = load_sale_invoice(client, ret)
    problems = []
    if invoice_state(nota) != "draft":
        problems.append(f"a nota não está pendente ({nota.get('descricao_situacao')})")
    net = (_decimal(venda.get("valor_produtos")) or Decimal(0)) - (_decimal(venda.get("valor_desconto")) or Decimal(0))
    total = _decimal(nota.get("valor_produtos")) or Decimal(0)
    if total > net + Decimal("0.02"):
        problems.append(f"devolução de {total} maior que a venda ({net})")
    simples = str(venda.get("regime_tributario") or "") in SIMPLES_REGIMES
    if simples != ("Simples" in str(nota.get("natureza_operacao") or "")):
        problems.append(f"natureza {nota.get('natureza_operacao')} não bate com o regime da venda")
    sold = {str(i.get("codigo")).strip(): i for i in unwrap_items(venda)}
    returned: dict[str, Decimal] = defaultdict(Decimal)
    for item in unwrap_items(nota):
        sku = str(item.get("codigo")).strip()
        returned[sku] += Decimal(str(item.get("quantidade") or 0))
        s = sold.get(sku)
        if not s:
            problems.append(f"{sku} não está na NF de venda")
            continue
        cfop = re.sub(r"\D", "", str(item.get("cfop") or ""))
        sale_cfop = re.sub(r"\D", "", str(s.get("cfop") or ""))
        if len(cfop) != 4 or cfop[0] not in "12" or cfop[1:] not in ("202", "201"):
            problems.append(f"CFOP {item.get('cfop')} de {sku}")
        elif sale_cfop[:1] == "5" and cfop[0] != "1" or sale_cfop[:1] == "6" and cfop[0] != "2":
            problems.append(f"CFOP {cfop} não corresponde à venda {sale_cfop}")
        if (_decimal(item.get("valor_unitario")) or 0) > (_decimal(s.get("valor_unitario")) or 0) + Decimal("0.01"):
            problems.append(f"{sku} com valor acima da venda")
    expected: dict[str, Decimal] = defaultdict(Decimal)
    for i in ret.items:
        expected[str(i.seller_sku).strip()] += Decimal(i.quantity or 1)
    if dict(returned) != dict(expected):
        problems.append("itens da nota diferentes da conferência")
    return problems


def emit_checked_drafts(db: Session, limit: int = 40) -> dict[str, int]:
    """Job: autoriza na Sefaz os rascunhos que o próprio app criou, depois de conferir contra a venda.

    Notas só vinculadas (emitidas fora do app, como as pendentes de setembro) nunca são autorizadas aqui.
    O estoque vem depois, pela rotina de notas (depósito certo).
    """
    s = get_settings()
    if s.olist_invoice_mode != "draft" or not s.olist_auto_emit:
        return {}
    rows = db.scalars(
        select(Return).where(
            Return.erp_return_invoice_status == "draft",
            Return.erp_return_invoice_id.is_not(None),
            or_(Return.erp_last_error.is_(None), *[Return.erp_last_error.like(f"%{t}%") for t in RETRYABLE]),
        ).order_by(Return.received_at).limit(limit)
    ).all()
    client = get_olist_client()
    result = {"autorizadas": 0, "barradas": 0, "erros": 0}
    for ret in rows:
        created = any(
            e.event_type == EventType.ERP_INVOICE_CREATED
            and (e.payload or {}).get("erp_invoice_id") == ret.erp_return_invoice_id
            for e in ret.events
        )
        if not created:
            continue
        try:
            problems = check_return_invoice(client, ret)
            if problems:
                _record_error(db, ret, ErpError("Não autorizada pela conferência automática: " + "; ".join(problems)))
                result["barradas"] += 1
                continue
            client.emit_invoice(ret.erp_return_invoice_id)
            ret.erp_last_error = None
            _event(ret, EventType.ERP_INVOICE_AUTHORIZED, f"NF {ret.erp_return_invoice_number} enviada à Sefaz "
                   "pela rotina, depois de conferida contra a venda")
            db.commit()
            result["autorizadas"] += 1
        except (ErpError, OlistError) as exc:
            db.rollback()
            _record_error(db, ret, exc)
            result["erros"] += 1
        time.sleep(3)
    return result


def check_invoices(db: Session) -> None:
    """Job: acompanha NFs em rascunho (emitidas no Olist pela equipe) e lança estoque pendente."""
    rows = db.scalars(
        select(Return).where(
            (Return.erp_return_invoice_status == "draft")
            | ((Return.erp_return_invoice_status == "authorized")
               & (Return.erp_stock_status.is_(None) | Return.erp_stock_status.in_((STOCK_RESERVED, STOCK_LAUNCHED))))
        )
    ).all()
    for ret in rows:
        try:
            if ret.erp_return_invoice_status == "draft":
                refresh_invoice_status(db, ret)
            else:
                post_stock(db, ret)
        except (ErpError, OlistError) as exc:
            db.rollback()
            _note_error(db, ret, exc)
