"""Defeito vira perda (NF de baixa por perda, CFOP 5.927): motivo e foto sempre registrados, lista por mês."""

from datetime import UTC, date, datetime
from decimal import Decimal
from io import BytesIO

import pytest
from openpyxl import load_workbook

from app.models import Return, ReturnItem
from app.services import excel, inspection, losses


class _DB:
    def __init__(self, media_count=0):
        self.media_count = media_count

    def scalar(self, stmt):
        return self.media_count

    def commit(self):
        pass


def _ret(**kw):
    ret = Return(platform="shopee", kind="customer_return", status="received", return_type="return_and_refund",
                 external_order_id="P1", external_return_id="R1", **kw)
    ret.items = [ReturnItem(seller_sku="VEST-M", product_name="Vestido", quantity=2)]
    ret.events = []
    return ret


def test_defect_needs_reason():
    with pytest.raises(inspection.InspectionError, match="qual é o defeito"):
        inspection.check_defect_evidence(_DB(media_count=1), _ret(), None, None)


def test_defect_other_needs_description():
    with pytest.raises(inspection.InspectionError, match="descreva"):
        inspection.check_defect_evidence(_DB(media_count=1), _ret(), "other", "  ")
    inspection.check_defect_evidence(_DB(media_count=1), _ret(), "other", "zíper quebrado")


def test_defect_needs_photo():
    with pytest.raises(inspection.InspectionError, match="foto do defeito"):
        inspection.check_defect_evidence(_DB(media_count=0), _ret(), "stain", None)
    inspection.check_defect_evidence(_DB(media_count=1), _ret(), "stain", None)


def test_reason_saved_only_for_defect():
    ret = _ret()
    inspection.receive_return(_DB(), ret, "Ana", "defect", "gola manchada", False, defect_reason="stain")
    assert ret.defect_reason == "stain"
    assert ret.events[-1].payload == {"condition": "defect", "defect_reason": "stain"}
    other = _ret()
    inspection.receive_return(_DB(), other, "Ana", "resale", None, False, defect_reason="stain")
    assert other.defect_reason is None


def test_reason_codes_match_screen_labels():
    from app.ui.labels import DEFECT_REASON

    assert tuple(DEFECT_REASON) == inspection.DEFECT_REASONS


def test_month_bounds_use_company_timezone():
    first, start, end = losses.month_bounds("2026-10", date(2026, 10, 28))
    assert first == date(2026, 10, 1)
    assert start == datetime(2026, 10, 1, 3, tzinfo=UTC)  # meia-noite em São Paulo
    assert end == datetime(2026, 11, 1, 3, tzinfo=UTC)
    assert losses.month_bounds("lixo", date(2026, 10, 28))[0] == date(2026, 10, 1)
    assert losses.month_bounds("2026-12", date(2026, 12, 5))[2] == datetime(2027, 1, 1, 3, tzinfo=UTC)
    assert losses.shift_month(date(2026, 1, 1), -1) == "2025-12"


def _row(reason, photos, sku="VEST-M", qty=2, order="P1"):
    ret = _ret(received_at=datetime(2026, 10, 27, 15, tzinfo=UTC), received_by="Ana", defect_reason=reason,
               cost_product_loss=Decimal("80.00"))
    ret.external_order_id = order
    items = [{"sku": sku, "name": "Vestido", "variant": "M", "quantity": qty, "amount": None}]
    return {"ret": ret, "items": items, "pieces": qty, "reason": reason, "notes": None, "cost": Decimal("80.00"),
            "photos": photos}


def test_summary_and_sku_totals_for_the_invoice():
    rows = [_row("stain", 2), _row("stain", 1, order="P2"), _row(None, 0, sku="BLS-P", qty=1, order="P3")]
    s = losses.summarize(rows)
    assert (s["returns"], s["pieces"], s["with_photo"], s["without_reason"]) == (3, 5, 2, 1)
    assert s["cost"] == Decimal("240.00") and s["top_reason"] == "stain"
    skus = losses.by_sku(rows)
    assert [(k["sku"], k["quantity"], k["orders"]) for k in skus] == [("VEST-M", 4, ["P1", "P2"]), ("BLS-P", 1, ["P3"])]


def test_losses_excel_has_invoice_and_proof_sheets():
    rows = [_row("tear", 1)]
    wb = load_workbook(BytesIO(excel.build_losses_xlsx("10-2026", rows, losses.by_sku(rows))))
    assert wb.sheetnames == ["Por SKU (NF 5.927)", "Peças 10-2026"]
    assert [c.value for c in wb["Por SKU (NF 5.927)"][2]][:4] == ["VEST-M", "Vestido", "M", 2]
    line = [c.value for c in wb["Peças 10-2026"][2]]
    assert line[6] == "Rasgo/furo" and line[8] == 1
    assert line[12].endswith(f"/app/devolucoes/{rows[0]['ret'].id}")


def test_cost_by_model_uses_longest_sku_prefix():
    from app.services import product_costs
    from app.services.costs import compute_costs

    product_costs.set_table({
        "VC": ("LN02", "Vestido em linho regata curto", Decimal("27.40")),
        "VCA": ("BR01", "Vestido com amarração liso curto", Decimal("28.00")),
        "VCA-PRETLIS": ("LT01", "Vestido com amarração listrado curto", Decimal("29.00")),
        "300": ("LN01", "Vestido em linho regata midi", Decimal("31.50")),
    })
    assert product_costs.model_for("vca-pretlis-g1")[0] == "LT01"  # listrado
    assert product_costs.model_for("VCA-MANTEI-G")[0] == "BR01"  # liso
    assert product_costs.model_for("VC-REG-CRU-M")[0] == "LN02"
    assert product_costs.model_for("300000015-CRU-M")[0] == "LN01"  # SKU antigo do ML, só números
    assert product_costs.model_for("VCX-AZUL-M") is None  # "VC" só casa até o "-"
    assert product_costs.unit_cost("VL-CHUMBO-G") == Decimal("40")  # sem modelo: custo padrão

    ret = _ret(item_condition="defect", condition_source="manual")
    ret.items = [ReturnItem(seller_sku="VCA-PRETLIS-M", quantity=2), ReturnItem(seller_sku="VL-CHUMBO-G", quantity=1)]
    compute_costs(ret)
    assert ret.cost_product_loss == Decimal("98.00")  # 2 × 29,00 + 1 × 40 (padrão)
