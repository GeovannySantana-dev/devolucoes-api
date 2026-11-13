"""Shopee em PROCESSING com o pacote já entregue fica como "Pacote entregue"."""

from app.enums import ReturnStatus
from app.models import Return
from app.services.sync import effective_status


def test_shopee_processing_delivered_is_received():
    ret = Return(platform="shopee", logistics_status="delivered")
    assert effective_status(ret, ReturnStatus.AWAITING_BUYER_SHIPMENT) == ReturnStatus.RECEIVED
    assert effective_status(Return(platform="shopee", logistics_status="in_transit"),
                            ReturnStatus.AWAITING_BUYER_SHIPMENT) == ReturnStatus.AWAITING_BUYER_SHIPMENT
    assert effective_status(Return(platform="tiktok", logistics_status="delivered"),
                            ReturnStatus.AWAITING_BUYER_SHIPMENT) == ReturnStatus.AWAITING_BUYER_SHIPMENT
