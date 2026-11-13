"""Custo confirmado das devoluções do TikTok pelo extrato financeiro (casos reais simplificados)."""

from datetime import UTC, datetime
from decimal import Decimal

from app.models import Return, ReturnItem
from app.services.costs import compute_costs
from app.services.tiktok_settlement import costs_from_statement


def _line(revenue, shipping, return_fee="0", fees="0"):
    return {"revenue_amount": revenue, "shipping_cost_amount": shipping, "fee_tax_amount": fees,
            "shipping_cost_breakdown": {"return_shipping_fee_amount": return_fee}}


# Pedido 586200000000000001: devolvido inteiro, frete de ida 14,10 + taxa 4,19 + frete de volta 27,25
FULL_ONE_LINE = {"revenue_amount": "0", "settlement_amount": "-45.54",
                 "sku_transactions": [_line("0", "-41.35", "-27.25", "-4.19")]}
# Pedido 586200000000000002: linha da venda + linha de estorno; desconto de frete estornado e taxas não devolvidas
FULL_TWO_LINES = {"revenue_amount": "0", "settlement_amount": "-60.38",
                  "sku_transactions": [_line("89.9", "0", "0", "-26.22"), _line("-89.9", "-45.55", "-34.75", "11.39")]}
# Pedido 586200000000000003: 4 peças, 1 devolvida (frete de volta 71,10 + desconto de frete estornado 10,40)
PARTIAL = {"revenue_amount": "159.8", "settlement_amount": "9.96",
           "sku_transactions": [_line("79.9", "-2.4"), _line("79.9", "-2.4"), _line("79.9", "-2.4"),
                                _line("-79.9", "-81.5", "-71.1")]}


def test_full_return_counts_whole_order_loss():
    assert costs_from_statement(FULL_ONE_LINE) == (Decimal("27.25"), Decimal("18.29"))
    assert costs_from_statement(FULL_TWO_LINES) == (Decimal("34.75"), Decimal("25.63"))


def test_partial_return_counts_only_refund_lines():
    assert costs_from_statement(PARTIAL) == (Decimal("71.10"), Decimal("10.40"))


def test_not_settled_yet_and_split_between_returns():
    assert costs_from_statement({"revenue_amount": "0", "settlement_amount": "0", "sku_transactions": []}) is None
    assert costs_from_statement(FULL_ONE_LINE, returns_in_order=2) == (Decimal("13.62"), Decimal("9.14"))


def _ret(raw=None):
    raw = raw or {"shipping_fee_amount": [{"seller_paid_return_shipping_fee": "32.50"}]}
    ret = Return(platform="tiktok", kind="customer_return", status="completed", return_type="return_and_refund",
                 item_condition="resale", condition_source="auto", raw=raw)
    ret.items = [ReturnItem(seller_sku="X", quantity=1)]
    return ret


def test_cost_uses_estimate_then_statement():
    ret = _ret()
    compute_costs(ret)
    assert ret.cost_return_shipping == Decimal("32.50") and ret.cost_lost_sale == 0
    assert ret.cost_total == Decimal("36.50")  # previsão + operacional

    ret.settled_return_shipping, ret.settled_lost_sale = Decimal("34.75"), Decimal("25.63")
    ret.cost_settled_at = datetime.now(UTC)
    compute_costs(ret)
    assert ret.cost_return_shipping == Decimal("34.75") and ret.cost_lost_sale == Decimal("25.63")
    assert ret.cost_total == Decimal("64.38") and ret.liability == "seller"


def test_platform_paid_return_still_counts_lost_sale():
    ret = _ret(raw={"shipping_fee_amount": [{"platform_paid_return_shipping_fee": "21.99"}]})
    ret.settled_return_shipping, ret.settled_lost_sale = Decimal("0"), Decimal("12.40")
    ret.cost_settled_at = datetime.now(UTC)
    compute_costs(ret)
    assert ret.liability == "platform" and ret.cost_return_shipping == 0
    assert ret.cost_total == Decimal("16.40")


# ---------- Shopee e Mercado Livre (casos reais) ----------

from app.services.settlement import ml_costs, shopee_costs  # noqa: E402


def test_shopee_escrow_rules():
    # 260907EXEMPLO1: frete reverso 15 + frete de ida 8,39 -> repasse -23,39
    assert shopee_costs({"buyer_total_amount": 69.6, "reverse_shipping_fee": 15, "escrow_amount": -23.39}) == (
        Decimal("15.00"), Decimal("8.39"))
    # Devolução Fácil cobriu tudo: repasse zero
    assert shopee_costs({"buyer_total_amount": 84.9}) == (Decimal("0.00"), Decimal("0.00"))
    # Devolução parcial: repasse positivo, só o frete reverso conta
    assert shopee_costs({"buyer_total_amount": 149.42, "reverse_shipping_fee": 0, "escrow_amount": 51.61}) == (
        Decimal("0.00"), Decimal("0.00"))
    assert shopee_costs(None) is None


def _bill(kind, amount, text="Custo por vender no Mercado Livre"):
    return {"charge_info": {"detail_type": kind, "detail_amount": amount, "transaction_detail": text}}


def test_ml_billing_rules():
    # 2000010000000008: tarifas estornadas, frete de ida 16,62 não
    details = [_bill("CHARGE", 17.94), _bill("CHARGE", 0.09), _bill("CHARGE", 16.62, "Tarifa de envio extra"),
               _bill("BONUS", 17.94), _bill("BONUS", 0.09)]
    assert ml_costs(details, Decimal("0")) == (Decimal("0.00"), Decimal("16.62"))
    # Tudo estornado; frete de devolução vem da reclamação
    details = [_bill("CHARGE", 28.9), _bill("BONUS", 28.9)]
    assert ml_costs(details, Decimal("23.50")) == (Decimal("23.50"), Decimal("0.00"))
    # Faturamento com lançamento de devolução separado
    details = [_bill("CHARGE", 20, "Tarifa de envio da devolução"), _bill("CHARGE", 5), _bill("BONUS", 5)]
    assert ml_costs(details, Decimal("99")) == (Decimal("20.00"), Decimal("0.00"))
    assert ml_costs([], Decimal("10")) is None


def test_settled_value_used_for_shopee():
    ret = Return(platform="shopee", kind="customer_return", status="completed", return_type="return_and_refund",
                 item_condition="resale", condition_source="auto", raw={})
    ret.items = [ReturnItem(seller_sku="X", quantity=1)]
    compute_costs(ret)
    assert ret.cost_total == Decimal("4.00")  # estimado: Devolução Fácil
    ret.settled_return_shipping, ret.settled_lost_sale = Decimal("15"), Decimal("8.39")
    ret.cost_settled_at = datetime.now(UTC)
    compute_costs(ret)
    assert ret.cost_total == Decimal("27.39") and ret.liability == "seller"
