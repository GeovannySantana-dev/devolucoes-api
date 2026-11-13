"""Custos por modelo (tela do gestor), nome das lojas, perda por modelo e comprador reincidente."""

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from app.models import Return, ReturnItem, Store
from app.services import buyers, losses, product_costs
from app.services.product_costs import CostError, parse_cost


def test_buyer_key_only_for_shopee_and_mercado_livre():
    assert buyers.buyer_key("shopee", {"user": {"username": " Maria.23 "}}) == "shopee:maria.23"
    assert buyers.buyer_key("shopee", {"user": {}}) is None
    assert buyers.buyer_key("mercado_livre", {"order": {"buyer": {"id": 123}}}) == "mercado_livre:123"
    claim = {"claim": {"players": [{"role": "respondent", "user_id": 9}, {"role": "complainant", "user_id": 77}]}}
    assert buyers.buyer_key("mercado_livre", claim) == "mercado_livre:77"
    assert buyers.buyer_key("tiktok", {"user_id": "x"}) is None
    assert buyers.buyer_key("shein", None) is None


def test_store_label_uses_display_name():
    s = Store(platform="mercado_livre", name="PC20260101000000")
    assert s.label == "PC20260101000000"
    s.display_name = "  Loja ML "
    assert s.label == "Loja ML"
    s.display_name = "   "
    assert s.label == "PC20260101000000"


def test_parse_cost_accepts_brazilian_format():
    assert parse_cost("31,50") == Decimal("31.50")
    assert parse_cost("R$ 1.035,5") == Decimal("1035.50")
    assert parse_cost("27.40") == Decimal("27.40")
    for bad in ("", "abc", "-1", "20000"):
        with pytest.raises(CostError):
            parse_cost(bad)


def test_prefix_rules():
    assert product_costs._clean_prefix(" vca-pretlis- ") == "VCA-PRETLIS"
    for bad in ("", "VC A", "-VC", "V%"):
        with pytest.raises(CostError):
            product_costs._clean_prefix(bad)


def _ret(condition, sku, qty=1, source="manual", reason=None):
    r = Return(platform="shopee", kind="customer_return", status="completed", return_type="return_and_refund",
               external_return_id="R", received_at=datetime(2026, 10, 10, tzinfo=UTC), item_condition=condition,
               condition_source=source, defect_reason=reason)
    r.items = [ReturnItem(seller_sku=sku, quantity=qty)]
    return r


class _DB:
    def __init__(self, rets):
        self.rets = rets

    def scalars(self, stmt):
        return self

    def all(self):
        return self.rets


def test_loss_by_model_rate_and_cost():
    product_costs.set_table({
        "VM": ("LN03", "Vestido em linho manga curta midi", Decimal("36.20")),
        "VR": ("LN01", "Vestido em linho regata midi", Decimal("31.50")),
    })
    rets = [_ret("defect", "VM-CRU-M", 2, reason="seam"), _ret("resale", "VM-CRU-G", 2), _ret("mismatch", "VR-AZ-M"),
            _ret("resale", "VR-AZ-G", 3), _ret("defect", "VR-AZ-P", source="auto"), _ret("defect", "XX-1")]
    rows = losses.by_model(_DB(rets), datetime(2026, 10, 1, tzinfo=UTC), datetime(2026, 11, 1, tzinfo=UTC))
    assert [(m["code"], m["lost"], m["cost"]) for m in rows] == [
        ("LN03", 2, Decimal("72.40")), ("—", 1, Decimal("40")), ("LN01", 1, Decimal("31.50"))]
    ln03 = rows[0]
    assert ln03["inspected"] == 4 and ln03["rate"] == 0.5 and ln03["reasons"] == [("Costura", 2)]
    assert rows[2]["inspected"] == 5 and rows[2]["reasons"] == [("item diferente", 1)]  # sugestão automática não conta


def test_screens_render():
    from tests.test_ui import _fake_return, _render, _user

    gestor = _user("gestor")
    models = [{"code": "LN01", "name": "Regata midi", "cost": Decimal("31.50"), "prefixes": ["VR", "300"], "updated_at": None}]
    cov = {"total": 3, "covered": 2, "by_model": {"LN01": 2}, "missing": [{"sku": "VL-X", "name": "Longo", "pieces": 1}]}
    html = _render("custos.html", gestor, models=models, cov=cov, last=None, default_cost=Decimal("40"))
    assert "66,67%" in html and "VL-X" in html and "31,50" in html and "/app/custos/inicio/VR/remover" in html

    other = _fake_return()
    other.item_condition = "mismatch"
    other.received_at = datetime(2026, 10, 1, tzinfo=UTC)
    ret = _fake_return()
    page = _render("detalhe.html", gestor, ret=ret, physical=True, can_inspect=False, can_decide=False, can_contest=False,
                   buyer_media=[], buyer_comment=None, invoice_mode="off", conditions={}, events=[], same_buyer=[other])
    assert "Este comprador já devolveu 1 vez · 1 com item diferente" in page and "item diferente" in page

    p = type("P", (), {"start": datetime(2026, 10, 1).date(), "end": datetime(2026, 10, 28).date(), "platform": None,
                       "store_id": None, "label": lambda self: "01/10/2026 a 28/10/2026"})()
    lst = _render("reincidentes.html", gestor, p=p, buyers_list=[{"platform": "shopee", "returns": [ret, other], "mismatch": 1}],
                  repeat_min=3)
    assert "01/10/2026 a 28/10/2026" in lst and "Comprador 1 · Shopee · 2 devoluções" in lst
