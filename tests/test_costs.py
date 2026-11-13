from decimal import Decimal

from app.models import Return, ReturnItem
from app.services.costs import compute_costs


def _ret(platform="tiktok", reason_fee=None, condition="resale", status="completed", return_type="return_and_refund",
         kind="customer_return", qty=1):
    ret = Return(platform=platform, kind=kind, status=status, return_type=return_type, item_condition=condition,
                 condition_source="manual",
                 raw={"shipping_fee_amount": [reason_fee]} if reason_fee else {})
    ret.items = [ReturnItem(seller_sku="X", quantity=qty)]
    return ret


SELLER = {"seller_paid_return_shipping_fee": "26.58", "platform_paid_return_shipping_fee": "0", "buyer_paid_return_shipping_fee": "0"}
PLATFORM = {"seller_paid_return_shipping_fee": "0", "platform_paid_return_shipping_fee": "21.99", "buyer_paid_return_shipping_fee": "0"}


def test_tiktok_wrong_color_seller_pays_shipping():
    r = _ret(reason_fee=SELLER)
    compute_costs(r)
    assert r.liability == "seller"
    assert r.cost_return_shipping == Decimal("26.58")
    assert r.cost_product_loss == 0
    assert r.cost_operational == Decimal("4.00")
    assert r.cost_total == Decimal("30.58")


def test_tiktok_size_platform_pays_only_operational():
    r = _ret(reason_fee=PLATFORM)
    compute_costs(r)
    assert r.liability == "platform"
    assert r.cost_total == Decimal("4.00")


def test_defect_adds_product_loss_per_unit():
    r = _ret(reason_fee=SELLER, condition="defect", qty=2)
    compute_costs(r)
    assert r.cost_product_loss == Decimal("80.00")
    assert r.cost_total == Decimal("110.58")


def test_shopee_easy_return_has_no_shipping():
    r = _ret(platform="shopee", condition="resale")
    compute_costs(r)
    assert r.cost_return_shipping == 0
    assert r.cost_total == Decimal("4.00")


def test_cancelled_or_refund_only_has_no_cost():
    for r in (_ret(reason_fee=SELLER, status="cancelled"), _ret(reason_fee=SELLER, return_type="refund_only")):
        compute_costs(r)
        assert r.cost_total == 0


def test_suggested_defect_has_no_product_loss_until_inspected():
    r = _ret(reason_fee=SELLER, condition="defect")
    r.condition_source = "auto"  # só a sugestão pelo motivo: ninguém viu a peça ainda
    compute_costs(r)
    assert r.cost_product_loss == 0 and r.cost_total == Decimal("30.58")
    r.condition_source = "manual"  # conferência confirmou o defeito
    compute_costs(r)
    assert r.cost_product_loss == Decimal("40.00") and r.cost_total == Decimal("70.58")

def test_recomputing_same_costs_does_not_touch_the_date():
    from datetime import UTC, datetime

    from app.models import Return

    ret = Return(platform="shopee", return_type="return_and_refund", status="in_transit", refund_amount=Decimal("10"), raw={})
    ret.items = []
    compute_costs(ret)
    first = ret.cost_computed_at
    assert first is not None
    ret.cost_computed_at = old = datetime(2026, 1, 1, tzinfo=UTC)
    compute_costs(ret)
    assert ret.cost_computed_at == old
    ret.cost_operational = Decimal("999")  # valor diferente do calculado: recalcula e atualiza a data
    compute_costs(ret)
    assert ret.cost_computed_at != old
