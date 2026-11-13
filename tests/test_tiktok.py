import hashlib
import hmac
import json
from decimal import Decimal
from types import SimpleNamespace

import httpx
import pytest

from app.enums import ReturnStatus, ReturnType
from app.platforms.base import PlatformError
from app.platforms.tiktok.client import TikTokAPIError, TikTokClient, generate_sign, verify_webhook_signature
from app.platforms.tiktok.mapper import approve_decision, map_return, reject_decision

SAMPLE_RETURN = {
    "return_id": "4035000000000000001",
    "order_id": "576400000000000001",
    "return_type": "RETURN_AND_REFUND",
    "return_status": "RETURN_OR_REFUND_REQUEST_PENDING",
    "return_reason": "ecom_order_delivered_refund_and_return_reason_wrong_item",
    "return_reason_text": "Item errado",
    "create_time": 1760000000,
    "update_time": 1760003600,
    "refund_amount": {"currency": "BRL", "refund_total": "89.90"},
    "seller_next_action_response": [{"action": "SELLER_RESPOND_REFUND", "deadline": 1760172800}],
    "return_line_items": [
        {
            "return_line_item_id": "1",
            "sku_id": "sku-1",
            "seller_sku": "CAM-AZ-M",
            "product_name": "Camiseta",
            "sku_name": "Azul / M",
            "refund_amount": {"currency": "BRL", "refund_total": "89.90"},
            "product_image": {"url": "https://img/1.jpg"},
        }
    ],
}


def test_generate_sign_matches_spec():
    params = {"app_key": "k", "timestamp": "1700000000", "shop_cipher": "c", "sign": "x", "access_token": "t"}
    body = '{"a":1}'
    # secret + path + params ordenados (sem sign/access_token) + body + secret
    expected_base = "secret" + "/path/x" + "app_keyk" + "shop_cipherc" + "timestamp1700000000" + body + "secret"
    expected = hmac.new(b"secret", expected_base.encode(), hashlib.sha256).hexdigest()
    assert generate_sign("/path/x", params, body, "secret") == expected


def test_webhook_signature():
    body = b'{"type":12}'
    sig = hmac.new(b"app-secret", b"app-key" + body, hashlib.sha256).hexdigest()
    assert verify_webhook_signature("app-key", "app-secret", body, sig)
    assert not verify_webhook_signature("app-key", "app-secret", body + b" ", sig)
    assert not verify_webhook_signature("app-key", "app-secret", body, "")


def test_map_return():
    nr = map_return(SAMPLE_RETURN)
    assert nr.external_return_id == "4035000000000000001"
    assert nr.status == ReturnStatus.PENDING_REVIEW
    assert nr.return_type == ReturnType.RETURN_AND_REFUND
    assert nr.refund_amount == Decimal("89.90")
    assert nr.currency == "BRL"
    assert nr.action_deadline is not None and nr.action_deadline.timestamp() == 1760172800
    assert nr.items[0].seller_sku == "CAM-AZ-M"
    assert nr.items[0].quantity == 1


def test_reason_translated_to_portuguese():
    nr = map_return({**SAMPLE_RETURN, "return_reason": "ecom_order_delivered_refund_and_return_reason_no_need",
                     "return_reason_text": "No longer needed"})
    assert nr.reason_text == "Arrependimento"
    assert nr.reason_code == "ecom_order_delivered_refund_and_return_reason_no_need"
    # Motivo desconhecido: mantém o texto do TikTok
    assert map_return(SAMPLE_RETURN).reason_text == "Item errado"


def test_unknown_status_is_preserved():
    nr = map_return({**SAMPLE_RETURN, "return_status": "SOMETHING_NEW"})
    assert nr.status == ReturnStatus.UNKNOWN
    assert nr.platform_status == "SOMETHING_NEW"


def test_decisions():
    assert approve_decision(ReturnType.REFUND_ONLY, ReturnStatus.PENDING_REVIEW) == "APPROVE_REFUND"
    assert approve_decision(ReturnType.RETURN_AND_REFUND, ReturnStatus.PENDING_REVIEW) == "APPROVE_RETURN"
    assert approve_decision(ReturnType.RETURN_AND_REFUND, ReturnStatus.IN_TRANSIT) == "APPROVE_RECEIVED_PACKAGE"
    assert reject_decision(ReturnType.REFUND_ONLY, ReturnStatus.PENDING_REVIEW) == "REJECT_REFUND"
    assert reject_decision(ReturnType.RETURN_AND_REFUND, ReturnStatus.RECEIVED) == "REJECT_RECEIVED_PACKAGE"


def _client(handler):
    return TikTokClient("app-key", "app-secret", "https://api.test", "https://auth.test",
                        http=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=1)


def test_request_signs_and_sends_exact_body():
    seen = {}

    def handler(req: httpx.Request):
        seen["req"] = req
        return httpx.Response(200, json={"code": 0, "data": {"return_orders": [SAMPLE_RETURN]}})

    data = _client(handler).search_returns("tok", "cipher", {"return_ids": ["1"]})
    req = seen["req"]
    params = dict(req.url.params)
    body = req.content.decode()
    assert req.headers["x-tts-access-token"] == "tok"
    assert params["shop_cipher"] == "cipher"
    assert json.loads(body) == {"return_ids": ["1"]}
    assert params["sign"] == generate_sign(req.url.path, params, body, "app-secret")
    assert data["return_orders"][0]["return_id"] == SAMPLE_RETURN["return_id"]


def _reject_adapter(handler, photos):
    from app.platforms.tiktok.adapter import TikTokAdapter

    return TikTokAdapter(_client(handler), lambda s: "tok", "pt-BR", evidence_provider=lambda ret: photos)


class _Store:
    name, shop_cipher = "Loja", "cipher"


def _ret():
    from app.models import Return

    return Return(id="r1", external_return_id="R1", return_type="return_and_refund", status="received")


def test_reject_uploads_photos_as_images():
    seen = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/images/upload"):
            seen.append(("upload", req.content))
            n = len([s for s in seen if s[0] == "upload"])
            return httpx.Response(200, json={"code": 0, "data": {"uri": f"tos-img-{n}", "width": 1920, "height": 1440}})
        seen.append(("reject", json.loads(req.content)))
        return httpx.Response(200, json={"code": 0, "data": {}})

    photos = [(f"f{i}.jpg", b"jpeg", "image/jpeg") for i in range(4)]
    _reject_adapter(handler, photos).reject(_Store(), _ret(), "seller_reject_reason_x", "Veio outro produto")
    uploads = [s for s in seen if s[0] == "upload"]
    assert len(uploads) == 3 and b'name="data"' in uploads[0][1] and b"DESCRIPTION_IMAGE" in uploads[0][1]
    body = next(s[1] for s in seen if s[0] == "reject")
    assert body["decision"] == "REJECT_RECEIVED_PACKAGE"
    assert body["images"][0] == {"image_id": "tos-img-1", "mime_type": "image/jpeg", "width": 1920, "height": 1440}


def test_missing_product_scope_rejects_without_images_and_warns():
    seen = []

    def handler(req: httpx.Request):
        if req.url.path.endswith("/images/upload"):
            return httpx.Response(200, json={"code": 105005, "message": "Access denied", "request_id": "x"})
        seen.append(json.loads(req.content))
        return httpx.Response(200, json={"code": 0, "data": {}})

    adapter = _reject_adapter(handler, [("f.jpg", b"j", "image/jpeg")])
    adapter.reject(_Store(), _ret(), "r", "c")  # não perde o prazo
    assert seen and "images" not in seen[0]
    assert "seller.product.basic" in adapter.last_warning


def test_request_raises_on_api_error():
    def handler(req):
        return httpx.Response(200, json={"code": 105001, "message": "invalid token", "request_id": "r1"})

    try:
        _client(handler).search_returns("tok", "cipher", {})
    except TikTokAPIError as exc:
        assert exc.code == 105001
    else:
        raise AssertionError("esperava TikTokAPIError")


def _app2(monkeypatch, key="key-2", secret="secret-2", service="service-2"):
    from app.config import get_settings

    s = get_settings()
    monkeypatch.setattr(s, "tiktok2_app_key", key)
    monkeypatch.setattr(s, "tiktok2_app_secret", secret)
    monkeypatch.setattr(s, "tiktok2_service_id", service)


def _signed(key: str, secret: str, body: bytes) -> str:
    return hmac.new(secret.encode(), key.encode() + body, hashlib.sha256).hexdigest()


def test_webhook_accepts_either_app_but_never_an_empty_secret(monkeypatch):
    from app.routers.webhooks import _tiktok_signature_ok

    body = b'{"shop_id": "1"}'
    assert _tiktok_signature_ok(body, _signed("app-key", "app-secret", body))
    assert not _tiktok_signature_ok(body, _signed("", "", body))  # segundo app não configurado

    _app2(monkeypatch)
    assert _tiktok_signature_ok(body, _signed("key-2", "secret-2", body))
    assert _tiktok_signature_ok(body, _signed("app-key", "app-secret", body))
    assert not _tiktok_signature_ok(body, _signed("key-2", "errado", body))


def test_each_store_uses_its_own_app(monkeypatch):
    from app.models import Store
    from app.platforms.base import PlatformError
    from app.platforms.registry import get_adapter, get_tiktok_client

    get_tiktok_client.cache_clear()
    fabrica = Store(platform="tiktok", name="Fábrica")  # sem valor: é o app de sempre
    assert get_adapter(None, fabrica).client.app_key == "app-key"

    loja2 = Store(platform="tiktok", name="Loja 2", tiktok_app=2)
    with pytest.raises(PlatformError, match="não configurado"):
        get_adapter(None, loja2)

    _app2(monkeypatch)
    assert get_adapter(None, loja2).client.app_key == "key-2"
    assert get_adapter(None, fabrica).client.app_key == "app-key"
    get_tiktok_client.cache_clear()


def test_authorization_records_which_app_authorized(monkeypatch):

    from app.services import tiktok_auth

    fake = SimpleNamespace(
        exchange_code=lambda code: SimpleNamespace(
            granted_scopes=None, seller_name="Loja 2", region="BR", access_token="a", refresh_token="r",
            access_token_expires_at=None, refresh_token_expires_at=None,
        ),
        get_authorized_shops=lambda token: [{"id": "2", "name": "Loja 2", "cipher": "c", "region": "BR"}],
    )
    pedidos = []
    monkeypatch.setattr(tiktok_auth, "get_tiktok_client", lambda app: pedidos.append(app) or fake)
    db = SimpleNamespace(scalar=lambda q: None, add=lambda o: None, commit=lambda: None)
    store = tiktok_auth.authorize_with_code(db, "code", 2)[0]
    assert pedidos == [2] and store.tiktok_app == 2


def test_authorize_link_of_second_app(monkeypatch):
    from fastapi import HTTPException

    from app.routers import auth_tiktok
    from app.ui import routes

    _app2(monkeypatch, service="")
    with pytest.raises(HTTPException):
        auth_tiktok.authorize_url_2()
    _app2(monkeypatch)
    assert "service_id=service-2" in routes._authorize_url(SimpleNamespace(platform="tiktok", tiktok_app=2))
    assert "service_id=service-2" not in routes._authorize_url(SimpleNamespace(platform="tiktok", tiktok_app=1))
