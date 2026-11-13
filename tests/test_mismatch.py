"""Conferência "item diferente do pedido": veio outro produto no pacote (ex.: tentativa de golpe).

Regra da empresa: toda devolução recebe NF de devolução; só a revenda volta ao estoque.
"""

from decimal import Decimal

import pytest

from app.models import Return, ReturnItem
from app.services import erp, inspection
from app.services.costs import compute_costs


class _DB:
    def __init__(self, media_count=0):
        self.media_count = media_count
        self.commits = 0

    def scalar(self, stmt):
        return self.media_count

    def commit(self):
        self.commits += 1


def _ret(**kw):
    ret = Return(platform="shopee", kind="customer_return", status="completed", return_type="return_and_refund",
                 external_order_id="P1", external_return_id="R1", **kw)
    ret.items = [ReturnItem(seller_sku="VEST-M", quantity=2)]
    ret.events = []
    return ret


def test_mismatch_requires_photo_evidence():
    with pytest.raises(inspection.InspectionError, match="foto"):
        inspection.receive_return(_DB(media_count=0), _ret(), "Ana", "mismatch", "Veio uma crocs", False)


def test_mismatch_with_photo_goes_to_invoice_like_other_conditions(monkeypatch):
    created = []
    monkeypatch.setattr(erp, "create_return_invoice", lambda db, ret, actor=None: created.append(ret))
    db, ret = _DB(media_count=2), _ret()
    inspection.receive_return(db, ret, "Ana", "mismatch", "Veio uma crocs", create_invoice=True)
    assert ret.received_at and ret.item_condition == "mismatch"
    assert created == [ret]  # NF de devolução emitida normalmente


def test_mismatch_authorized_invoice_does_not_post_stock(monkeypatch):
    def no_olist():
        raise AssertionError("não deveria chamar o Olist para lançar estoque")

    monkeypatch.setattr(erp, "get_olist_client", no_olist)
    # Sem depósito de defeitos configurado (o .env local pode ter um)
    settings = erp.get_settings().model_copy(update={"olist_defect_deposit_id": None})
    monkeypatch.setattr(erp, "get_settings", lambda: settings)
    ret = _ret(item_condition="mismatch", erp_return_invoice_id="9", erp_return_invoice_status="authorized")
    erp.post_stock(_DB(), ret)
    assert ret.erp_stock_status == "skipped_mismatch"


def test_mark_handled_outside_and_undo():
    from types import SimpleNamespace

    rets = [_ret(), _ret()]
    db = _DB()
    db.scalars = lambda stmt: rets
    db.flush = lambda: None
    from datetime import UTC, datetime

    assert inspection.mark_handled_outside(db, datetime(2026, 10, 16, tzinfo=UTC), "Gestor") == 2
    assert all(r.handled_outside_at and r.handled_outside_by == "Gestor" for r in rets)
    assert rets[0].events[-1].event_type == "handled_outside"
    inspection.undo_handled_outside(db, rets[0], "Ana")
    assert rets[0].handled_outside_at is None


def test_cleanup_cutoff_includes_chosen_day():
    from app.ui.routes import _cutoff

    day, cutoff = _cutoff("2026-10-15")
    assert str(day) == "2026-10-15"
    assert cutoff.isoformat() == "2026-10-16T00:00:00-03:00"


def test_mismatch_counts_as_product_loss():
    ret = _ret(item_condition="mismatch", condition_source="manual")  # confirmado na conferência
    compute_costs(ret)
    assert ret.cost_product_loss == Decimal("80.00")  # 2 peças × custo padrão 40


def test_cleanup_only_closed_keeps_in_progress():
    from datetime import UTC, datetime

    from app.services.inspection import _not_inspected_before
    from app.enums import ReturnStatus
    cutoff = datetime(2026, 10, 1, tzinfo=UTC)
    assert len(_not_inspected_before(cutoff, only_closed=True)) == len(_not_inspected_before(cutoff)) + 1
    extra = _not_inspected_before(cutoff, only_closed=True)[-1]
    assert str(extra.compile(compile_kwargs={"literal_binds": True})).endswith(f"'{ReturnStatus.COMPLETED.value}'")
