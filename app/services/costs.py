# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Custo de cada devolução para a empresa.

custo_total = frete de devolução pago pela loja
            + venda perdida (TikTok, pelo extrato: frete de ida e taxas que não voltam)
            + perda da peça (defeito ou item diferente do pedido: custo do modelo × quantidade, pela
              planilha de custo; SKU sem modelo usa o custo padrão), só depois
              que a conferência confirmou a condição; a sugestão automática pelo motivo não conta
            + custo operacional (devolução com retorno físico)

O reembolso em si não entra: para peças que voltam ao estoque ele é receita devolvida,
reportada à parte nos relatórios.
"""

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

from ..config import Settings, get_settings
from ..enums import LOSS_CONDITIONS, Platform, ReturnKind, ReturnStatus, ReturnType
from ..models import Return
from .product_costs import unit_cost

CENT = Decimal("0.01")

# Devoluções sem retorno físico de peça ou que não chegaram a acontecer
_NO_PHYSICAL_RETURN = {ReturnStatus.CANCELLED}


def _d(value: Any) -> Decimal:
    try:
        return Decimal(str(value)) if value not in (None, "") else Decimal(0)
    except ArithmeticError:
        return Decimal(0)


def _tiktok_shipping(raw: dict[str, Any]) -> tuple[Decimal, Decimal, Decimal]:
    """(loja, plataforma, cliente) pagos no frete de devolução, segundo o TikTok."""
    fees = raw.get("shipping_fee_amount") or []
    if isinstance(fees, dict):
        fees = [fees]
    seller = sum((_d(f.get("seller_paid_return_shipping_fee")) for f in fees), Decimal(0))
    platform = sum((_d(f.get("platform_paid_return_shipping_fee")) for f in fees), Decimal(0))
    buyer = sum((_d(f.get("buyer_paid_return_shipping_fee")) for f in fees), Decimal(0))
    return seller, platform, buyer


def _has_physical_return(ret: Return) -> bool:
    if ret.status in _NO_PHYSICAL_RETURN:
        return False
    if ret.kind == ReturnKind.UNDELIVERED:
        return True
    return ret.return_type != ReturnType.REFUND_ONLY


def _pieces_cost(ret: Return) -> Decimal:
    """Custo das peças perdidas: custo do modelo de cada SKU × quantidade."""
    if not ret.items:
        return unit_cost(None)
    return sum((unit_cost(i.seller_sku) * Decimal(i.quantity or 1) for i in ret.items), Decimal(0))


def compute_costs(ret: Return, settings: Settings | None = None) -> None:
    s = settings or get_settings()
    physical = _has_physical_return(ret)

    shipping = Decimal(0)
    liability = "none"
    lost_sale = Decimal(0)
    if ret.platform == Platform.TIKTOK and ret.kind != ReturnKind.UNDELIVERED:
        seller, platform, buyer = _tiktok_shipping(ret.raw or {})
        shipping = seller
        if ret.cost_settled_at and ret.settled_return_shipping is not None:
            # Devolução fechada e pedido liquidado: vale o extrato financeiro (custo confirmado)
            shipping = ret.settled_return_shipping
            lost_sale = ret.settled_lost_sale or Decimal(0)
            seller = max(seller, shipping)
        if seller > 0:
            liability = "seller"
        elif platform > 0:
            liability = "platform"
        elif buyer > 0:
            liability = "buyer"
    elif ret.platform == Platform.SHOPEE and physical:
        shipping = Decimal(str(s.cost_shopee_return_shipping))
        liability = "platform"  # Devolução Fácil cobre o frete
    elif ret.platform == Platform.SHEIN and physical:
        # Frete de devolução cobrado da loja, por peça (returnExpense no detalhe da devolução)
        goods = (ret.raw or {}).get("returnGoodsInfoList") or []
        shipping = sum((_d(g.get("returnExpense")) for g in goods), Decimal(0))
        liability = "seller" if shipping > 0 else "platform"
    elif ret.platform == Platform.MERCADO_LIVRE and physical:
        # Valor que o Mercado Livre informa que cobra da loja pelo frete de devolução
        cost = (ret.raw or {}).get("return_cost")
        if cost is not None:
            shipping = _d(cost.get("amount"))
            liability = "seller" if shipping > 0 else "platform"
    if ret.platform != Platform.TIKTOK and ret.cost_settled_at and ret.settled_return_shipping is not None:
        # Shopee e Mercado Livre: devolução fechada e financeiro conferido (custo confirmado)
        shipping = ret.settled_return_shipping
        lost_sale = ret.settled_lost_sale or Decimal(0)
        if shipping > 0:
            liability = "seller"
    if ret.kind == ReturnKind.UNDELIVERED:
        liability = "none"  # transportadora/plataforma; sem frete reverso cobrado conhecido

    if not physical:
        shipping = Decimal(0)

    product_loss = (
        _pieces_cost(ret)
        if physical and ret.item_condition in LOSS_CONDITIONS and ret.condition_source == "manual"
        else Decimal(0)
    )
    operational = Decimal(str(s.cost_operational_per_return)) if physical else Decimal(0)

    values = {
        "liability": liability,
        "cost_return_shipping": shipping.quantize(CENT),
        "cost_product_loss": product_loss.quantize(CENT),
        "cost_operational": operational.quantize(CENT),
        "cost_lost_sale": lost_sale.quantize(CENT),
        "cost_total": (shipping + product_loss + operational + lost_sale).quantize(CENT),
    }
    # Sem mudança, não grava: a data nova marcaria a devolução como atualizada e a integração baixaria tudo de novo
    if ret.cost_computed_at is None or any(getattr(ret, k) != v for k, v in values.items()):
        for k, v in values.items():
            setattr(ret, k, v)
        ret.cost_computed_at = datetime.now(UTC)
