import hashlib
import hmac
import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.enums import ReturnStatus, ReturnType
from app.models import Return
from app.platforms.base import PlatformError
from app.platforms.shopee.adapter import ShopeeAdapter
from app.platforms.shopee.client import MAX_WINDOW, ShopeeAPIError, ShopeeClient, sign
from app.platforms.shopee.mapper import map_return, map_status
from app.services.erp import classify

SAMPLE_RETURN = {
    "return_sn": "2410150ABCDEF",
    "order_sn": "241010XYZ",
    "status": "ACCEPTED",
    "logistics_status": "LOGISTICS_PICKUP_DONE",
    "needs_logistics": True,
    "reason": "ITEM_NOT_FIT",
    "text_reason": "",
    "refund_amount": 79.9,
    "currency": "BRL",
    "create_time": 1760000000,
    "update_time": 1760003600,
    "due_date": 1760172800,
    "tracking_number": "BR123",
    "item": [
        {
            "item_id": 111,
            "model_id": 222,
            "name": "Vestido Midi",
            "item_sku": "VEST",
            "variation_sku": "VEST-AZ-M",
            "amount": 1,
            "item_price": 79.9,
            "images": ["https://img/1.jpg"],
        }
    ],
}


def test_sign_shop_call():
    expected = hmac.new(b"key", b"1/api/v2/x1700000000tok42", hashlib.sha256).hexdigest()
    assert sign("key", 1, "/api/v2/x", 1700000000, "tok", 42) == expected


def test_map_return_accepted_in_transit():
    nr = map_return(SAMPLE_RETURN)
    assert nr.external_return_id == "2410150ABCDEF"
    assert nr.external_order_id == "241010XYZ"
    # Devolução Fácil: a Shopee já aceitou; o físico segue pela conferência
    assert nr.status == ReturnStatus.COMPLETED
    assert nr.platform_status == "ACCEPTED / LOGISTICS_PICKUP_DONE"
    assert nr.return_type == ReturnType.RETURN_AND_REFUND
    assert nr.reason_text == "Não serviu"
    assert nr.refund_amount == Decimal("79.9")
    assert nr.items[0].seller_sku == "VEST-AZ-M"
    assert nr.items[0].refund_amount == Decimal("79.9")
    assert nr.action_deadline.timestamp() == 1760172800


def test_reason_is_standard_label_and_comment_stays_separate():
    from app.models import Return
    from app.services.erp import apply_suggested_condition, buyer_comment

    nr = map_return({**SAMPLE_RETURN, "text_reason": "Ficou grande e veio com mancha na manga"})
    assert nr.reason_text == "Não serviu"  # agrupa nos relatórios
    ret = Return(reason_code=nr.reason_code, reason_text=nr.reason_text, raw=nr.raw, kind="customer_return")
    assert buyer_comment(ret) == "Ficou grande e veio com mancha na manga"
    # O comentário entra na sugestão: "mancha" (defeito) vence "não serviu" (revenda)
    apply_suggested_condition(ret, [("não serviu", "resale", 50), ("mancha", "defect", 10)])
    assert ret.item_condition == "defect"
    # Sem código conhecido, o texto livre ainda serve de motivo
    assert map_return({**SAMPLE_RETURN, "reason": "ALGO", "text_reason": "Outra coisa"}).reason_text == "Outra coisa"


def test_refund_only_without_logistics():
    nr = map_return({**SAMPLE_RETURN, "needs_logistics": False, "logistics_status": None})
    assert nr.return_type == ReturnType.REFUND_ONLY
    assert nr.platform_status == "ACCEPTED"


@pytest.mark.parametrize(
    "status,logistics,expected",
    [
        ("REQUESTED", None, ReturnStatus.PENDING_REVIEW),
        ("PROCESSING", None, ReturnStatus.AWAITING_BUYER_SHIPMENT),
        ("PROCESSING", "LOGISTICS_PICKUP_DONE", ReturnStatus.IN_TRANSIT),
        ("PROCESSING", "LOGISTICS_DELIVERY_DONE", ReturnStatus.RECEIVED),
        ("ACCEPTED", "LOGISTICS_DELIVERY_DONE", ReturnStatus.COMPLETED),
        ("JUDGING", None, ReturnStatus.DISPUTED),
        ("CANCELLED", "LOGISTICS_REQUEST_CANCELED", ReturnStatus.CANCELLED),
        ("ALGO_NOVO", None, ReturnStatus.UNKNOWN),
    ],
)
def test_map_status(status, logistics, expected):
    assert map_status(status, logistics) == expected


def test_processing_waiting_seller_response_needs_action():
    assert map_status("PROCESSING", None, "PENDING_RESPOND") == ReturnStatus.PENDING_REVIEW


RULES = [("defeito", "defect", 10), ("danificado", "defect", 10), ("não serviu", "resale", 50),
         ("errado", "resale", 60), ("arrependimento", "resale", 60)]


@pytest.mark.parametrize(
    "reason,expected",
    [("ITEM_NOT_FIT", "resale"), ("FUNCTIONAL_DMG", "defect"), ("PHYSICAL_DMG", "defect"),
     ("WRONG_ITEM", "resale"), ("CHANGE_MIND", "resale")],
)
def test_reason_labels_feed_classification(reason, expected):
    nr = map_return({**SAMPLE_RETURN, "reason": reason})
    assert classify(nr.reason_code, nr.reason_text, RULES) == expected


def _client(handler):
    return ShopeeClient(99, "partner-key", "https://api.test",
                        http=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=1)


def test_request_signs_shop_call():
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"error": "", "response": SAMPLE_RETURN})

    r = _client(handler).get_return("tok", "42", "2410150ABCDEF")
    params = dict(seen["req"].url.params)
    assert params["partner_id"] == "99" and params["shop_id"] == "42" and params["access_token"] == "tok"
    assert params["return_sn"] == "2410150ABCDEF"
    assert params["sign"] == sign("partner-key", 99, "/api/v2/returns/get_return_detail", params["timestamp"], "tok", "42")
    assert r["return_sn"] == "2410150ABCDEF"


def test_request_raises_on_api_error():
    def handler(req):
        return httpx.Response(200, json={"error": "error_auth", "message": "Invalid access_token", "request_id": "r1"})

    with pytest.raises(ShopeeAPIError) as exc:
        _client(handler).get_return("tok", "42", "x")
    assert exc.value.error == "error_auth"


def test_missing_config_is_clear():
    client = ShopeeClient(None, "", "https://api.test", http=httpx.Client(transport=httpx.MockTransport(lambda r: None)))
    with pytest.raises(PlatformError, match="SHOPEE_PARTNER_ID"):
        client.get_return("tok", "42", "x")


def test_iter_returns_paginates_and_uses_15_day_windows():
    calls = []

    def handler(req: httpx.Request):
        p = dict(req.url.params)
        calls.append((int(p["update_time_from"]), int(p["update_time_to"]), int(p["page_no"])))
        first_page = p["page_no"] == "0" and len(calls) == 1
        return httpx.Response(200, json={"error": "", "response": {
            "return": [{**SAMPLE_RETURN, "return_sn": f"R{len(calls)}"}], "more": first_page}})

    until = datetime(2026, 10, 15, tzinfo=UTC)
    since = until - timedelta(days=20)
    rows = list(_client(handler).iter_returns("tok", "42", since, until))

    assert [c[2] for c in calls] == [0, 1, 0]  # page_no é o nº da página
    assert calls[0][1] - calls[0][0] == int(MAX_WINDOW.total_seconds())
    assert calls[-1][1] == int(until.timestamp())
    assert len(rows) == 3


def test_authorize_url_signs_without_token():
    url = ShopeeClient(99, "partner-key", "https://api.test").authorize_url("https://x/cb?state=s")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["redirect"] == "https://x/cb?state=s"
    assert q["sign"] == sign("partner-key", 99, "/api/v2/shop/auth_partner", q["timestamp"])


REAL_DISPUTE_REASONS = {"dispute_reason_list": [
    {"dispute_reason": 84, "dispute_requirement": "Você pode anexar fotos...",
     "evidence_module_list": [{"module_index": 1, "is_required": True,
                               "requirement": "Envie imagens que mostrem que o item retornado é diferente do item no anúncio"}]},
    {"dispute_reason": 90, "dispute_requirement": "Sem evidência", "evidence_module_list": []},
]}


class _S:
    name, external_shop_id = "Loja", "42"


def _dispute_api(calls):
    """API falsa da Shopee: motivos reais, convert_image devolve uma URL por foto, registra o dispute."""

    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/get_return_dispute_reason"):
            return httpx.Response(200, json={"error": "", "response": REAL_DISPUTE_REASONS})
        if path.endswith("/convert_image"):
            calls.append(("convert", req.content))
            n = sum(1 for c in calls if c[0] == "convert")
            return httpx.Response(200, json={"error": " ", "message": "success",
                                             "response": {"url": f"https://fileproxy/{n}.jpg"}})
        if path.endswith("/dispute"):
            calls.append(("dispute", json.loads(req.content)))
            return httpx.Response(200, json={"error": " ", "response": {"msg": "dispute return success"}})
        raise AssertionError(path)

    return handler


PHOTOS = [(f"foto{i}.jpg", b"\xff\xd8jpeg" + bytes([i]), "image/jpeg") for i in range(5)]


def test_reject_reasons_real_format_and_evidence_flag():
    adapter = ShopeeAdapter(_client(_dispute_api([])), lambda s: "tok", "sac@loja.com")
    reasons = {r.code: r.label for r in adapter.reject_reasons(_S(), Return(external_return_id="R1"))}
    assert "diferente do item no anúncio" in reasons["84"] and "envia as fotos" in reasons["84"]
    assert reasons["90"] == "Sem evidência"


def test_dispute_uploads_up_to_3_photos_into_required_module():
    calls = []
    adapter = ShopeeAdapter(_client(_dispute_api(calls)), lambda s: "tok", "sac@loja.com",
                            evidence_provider=lambda ret: PHOTOS)
    adapter.reject(_S(), Return(external_return_id="R1"), "84", "Veio uma crocs no lugar do vestido")

    converts = [c for c in calls if c[0] == "convert"]
    assert len(converts) == 3  # limite da Shopee por módulo
    assert b'name="return_sn"' in converts[0][1] and b'name="upload_image"' in converts[0][1]
    body = next(c[1] for c in calls if c[0] == "dispute")
    assert body["dispute_reason_id"] == 84 and "dispute_reason" not in body
    assert body["email"] == "sac@loja.com"
    assert body["image_list"] == [{"module_index": 1, "requirement": REAL_DISPUTE_REASONS["dispute_reason_list"][0]
                                   ["evidence_module_list"][0]["requirement"],
                                   "image_url": ["https://fileproxy/1.jpg", "https://fileproxy/2.jpg",
                                                 "https://fileproxy/3.jpg"]}]


def test_dispute_without_photos_is_blocked_when_evidence_required():
    adapter = ShopeeAdapter(_client(_dispute_api([])), lambda s: "tok", "sac@loja.com")
    with pytest.raises(PlatformError, match="adicione fotos"):
        adapter.reject(_S(), Return(external_return_id="R1"), "84", "x")


def test_dispute_reason_without_evidence_sends_no_images_and_needs_email():
    calls = []
    with pytest.raises(PlatformError, match="SHOPEE_DISPUTE_EMAIL"):
        ShopeeAdapter(_client(_dispute_api(calls)), lambda s: "tok", "").reject(_S(), Return(), "90", "x")
    ShopeeAdapter(_client(_dispute_api(calls)), lambda s: "tok", "sac@loja.com").reject(
        _S(), Return(external_return_id="R1"), "90", "Produto usado")
    body = next(c[1] for c in calls if c[0] == "dispute")
    assert body["dispute_reason_id"] == 90 and "image_list" not in body
    with pytest.raises(PlatformError, match="não está disponível"):
        ShopeeAdapter(_client(_dispute_api(calls)), lambda s: "tok", "sac@loja.com").reject(
            _S(), Return(external_return_id="R1"), "999", "x")


def test_contest_allowed_after_shopee_accepted():
    from app.services.actions import can_contest_after_accept

    assert can_contest_after_accept(Return(platform="shopee", platform_status="ACCEPTED"))
    assert not can_contest_after_accept(Return(platform="tiktok", platform_status="ACCEPTED"))
