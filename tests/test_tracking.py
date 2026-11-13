from datetime import UTC, datetime

from app.models import Return
from app.services.tracking import apply_tracking, map_shopee_tracking

# Resposta real (resumida) de returns/get_reverse_tracking_info, 16/10/2026
REAL = {
    "return_sn": "2610050EXEMP001",
    "tracking_number": "BR100000000001F",
    "reverse_logistics_status": "LOGISTICS_DELIVERY_DONE",
    "reverse_logistics_update_time": 1791556000,
    "tracking_info": [
        {"update_time": 1791544809, "tracking_description": "O pedido chegou ao centro de  e segue para a próxima etapa."},
        {"update_time": 1791555999, "tracking_description": "Pedido devolvido"},
    ],
}


def test_map_real_delivered():
    data = map_shopee_tracking(REAL)
    assert data["logistics_status"] == "delivered"
    assert data["logistics_detail"] == "Pedido devolvido"  # movimentação mais recente
    assert data["logistics_event_at"] == datetime.fromtimestamp(1791555999, UTC)
    assert data["tracking_number"] == "BR100000000001F"


def test_map_statuses_and_text_fallback():
    assert map_shopee_tracking({"reverse_logistics_status": "LOGISTICS_PICKUP_DONE"})["logistics_status"] == "in_transit"
    assert map_shopee_tracking({"reverse_logistics_status": "LOGISTICS_PENDING_ARRANGE"})["logistics_status"] == "awaiting_shipment"
    unknown = {"reverse_logistics_status": "ALGO_NOVO",
               "tracking_info": [{"update_time": 1, "tracking_description": "Aguardando postagem pelo remetente"}]}
    assert map_shopee_tracking(unknown)["logistics_status"] == "awaiting_shipment"
    moving = {"tracking_info": [{"update_time": 1, "tracking_description": "Objeto em trânsito"}]}
    assert map_shopee_tracking(moving)["logistics_status"] == "in_transit"


def test_apply_records_event_only_on_change_and_fills_tracking():
    ret = Return(tracking_number=None)
    ret.events = []
    now = datetime.now(UTC)
    assert apply_tracking(ret, map_shopee_tracking(REAL), now)
    assert ret.tracking_number == "BR100000000001F" and ret.logistics_checked_at == now
    assert len(ret.events) == 1 and ret.events[0].event_type == "tracking"
    assert not apply_tracking(ret, map_shopee_tracking(REAL), now)  # sem mudança, sem evento novo
    assert len(ret.events) == 1
