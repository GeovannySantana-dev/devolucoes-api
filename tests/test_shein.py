"""SHEIN: assinatura, decifragem da chave da loja e link de autorização."""

import base64
import hashlib
import hmac

from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from app.platforms.shein.client import IV, SheinClient, decrypt_secret, sign


def test_signature_follows_shein_rule():
    s = sign("APPID123", "SECRET", "/open-api/auth/get-by-token", "1700000000000", random_key="abcde")
    expected = hmac.new(b"SECRETabcde", b"APPID123&1700000000000&/open-api/auth/get-by-token", hashlib.sha256).hexdigest()
    assert s == "abcde" + base64.b64encode(expected.encode()).decode()
    assert len(sign("A", "B", "/x", "1")) > 5  # random de 5 caracteres na frente


def test_decrypt_store_secret_like_shein():
    app_secret = "00112233445566778899AABBCCDDEEFF"
    padder = padding.PKCS7(128).padder()
    data = padder.update(b"EXEMPLO000000001") + padder.finalize()
    enc = Cipher(algorithms.AES(app_secret.encode()[:16]), modes.CBC(IV)).encryptor()
    encrypted = base64.b64encode(enc.update(data) + enc.finalize()).decode()
    assert decrypt_secret(encrypted, app_secret) == "EXEMPLO000000001"


def test_authorize_url_has_base64_redirect():
    c = SheinClient("APP1", "SEC", "https://openapi.sheincorp.com", "https://openapi-sem.sheincorp.com/#/empower")
    url = c.authorize_url("https://app.exemplo.com/auth/shein/callback", "st")
    redirect = base64.b64encode(b"https://app.exemplo.com/auth/shein/callback").decode()
    assert url == f"https://openapi-sem.sheincorp.com/#/empower?appid=APP1&redirectUrl={redirect}&state=st"

# ---------- devoluções (mapeamento com dados reais simplificados) ----------

from datetime import datetime, timedelta, timezone  # noqa: E402
from decimal import Decimal  # noqa: E402

from app.enums import ReturnStatus, ReturnType  # noqa: E402
from app.platforms.shein.adapter import SheinAdapter  # noqa: E402
from app.platforms.shein.mapper import main_reason, map_return  # noqa: E402

DETAIL = {
    "returnOrderNo": "19EXMP0001", "returnOrderStatus": 7, "noReturnGoodsSign": 0, "orderNo": "GSH19W100000001",
    "platformExpressNo": "AP100000002BR", "expressCompanyName": "Prepaid Return Shipping Code",
    "requestReturnTime": "2026-10-20 06:27:25", "lastUpdateTime": "2026-10-21 02:28:38",
    "returnGoodsInfoList": [{
        "goodsId": 4300000001, "sku": "I1exemplo0000a", "goodsSn": "VA-MARRANT", "skuSn": "VCA-AZCLARO-GG",
        "currency": "BRL", "goodsTitle": "Vestido Curto Marrant", "imageUrl": "https://img/x.jpg",
        "commodityAttributeList": [{"attrName": "Sky Blue-GG", "language": "US"}, {"attrName": "azul celeste-GG", "language": "PT"}],
        "sellerCurrencyPrice": 74.9, "sellerCurrencyPromotionPrice": 7.49, "sellerCurrencyStoreCouponPrice": 0.0,
        "returnExpense": 0,
        "returnReasonList": [{"language": "EN", "reason": "Size too bigOverall fit is too big"},
                             {"language": "PT", "reason": "Tamanho muito grandeO ajuste geral é muito grande"}],
    }],
}


def test_map_shein_return():
    r = map_return(DETAIL)
    assert r.status == ReturnStatus.IN_TRANSIT and r.platform_status == "Entrega pendente"
    assert r.return_type == ReturnType.RETURN_AND_REFUND and r.external_order_id == "GSH19W100000001"
    assert r.reason_text == "Tamanho muito grande" and r.reason_code.startswith("Size too big")
    assert r.refund_amount == Decimal("67.41") and r.tracking_number == "AP100000002BR"
    assert r.requested_at == datetime(2026, 10, 19, 22, 27, 25, tzinfo=timezone.utc)  # GMT+8 -> UTC
    assert r.action_deadline is None
    item = r.items[0]
    assert item.seller_sku == "VCA-AZCLARO-GG" and item.sku_name == "azul celeste-GG" and item.quantity == 1


def test_main_reason_and_refund_only():
    assert main_reason("Problemas de qualidade do produtoDanificado/fios puxados") == "Problemas de qualidade do produto"
    assert main_reason("Arrependimento") == "Arrependimento"
    assert map_return({**DETAIL, "noReturnGoodsSign": 1, "returnOrderStatus": 9}).return_type == ReturnType.REFUND_ONLY


class _FakeClient:
    def __init__(self):
        self.calls = []

    def store_request(self, path, body, key, secret):
        self.calls.append((path, body))
        if path.endswith("/list"):
            return {"count": 1, "returnOrderList": [{"returnOrderNo": "19EXMP0001"}]}
        return [DETAIL]


def test_adapter_splits_into_48h_windows_and_never_decides():
    from types import SimpleNamespace

    import pytest

    from app.platforms.base import PlatformError

    client = _FakeClient()
    store = SimpleNamespace(name="Shein", access_token="k", refresh_token="s")
    start = datetime(2026, 10, 1, tzinfo=timezone.utc)
    rows = list(SheinAdapter(client).fetch_returns(store, start, start + timedelta(days=5)))
    lists = [b for p, b in client.calls if p.endswith("/list")]
    assert len(lists) == 3 and all(b["queryType"] == 3 and b["pageSize"] == 30 for b in lists)
    assert len(rows) == 3
    with pytest.raises(PlatformError, match="automaticamente"):
        SheinAdapter(client).approve(store, None)


def test_known_errors_explain_what_to_do():
    import httpx
    import pytest

    from app.platforms.base import PlatformError

    def reply(code, msg):
        return httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, json={"code": code, "msg": msg})))

    c = SheinClient("APP1", "SEC", "https://openapi.sheincorp.com", "https://x", http=reply("openapi00002", "IP is not in the whitelist"))
    with pytest.raises(PlatformError, match="lista branca"):
        c._post("/open-api/return-order/list", {}, {})
    c = SheinClient("APP1", "SEC", "https://openapi.sheincorp.com", "https://x", http=reply("openapi00001", "签名错误"))
    with pytest.raises(PlatformError, match="Autorize a loja Shein de novo"):
        c._post("/open-api/return-order/list", {}, {})


# ---------- extrato financeiro e confirmação de recebimento ----------

from app.services.shein_settlement import group_lines, shein_costs  # noqa: E402


def _line(no, kind, value, check=None):
    return {"checkOrderNo": check or f"D{no}{kind}", "bzOrderNo": no, "secondOrderType": kind, "estimateIncomeMoneyTotal": value}


def test_shein_costs_from_real_statement():
    # Números reais: venda 79,90 - 15,98 comissão - 5,00 envio = 58,92; a devolução tira 63,92 e cobra 10,00
    lines = [_line("GSH19M00EXEMPL", 1, 52.53), _line("19EXEMPLO1", 2, 57.53), _line("19EXEMPLO1", 13, 10.0),
             _line("19EXEMPLO1", 13, 10.0, check="D19EXEMPLO113")]  # repetida (duas janelas): conta uma vez
    by_no = group_lines(lines)
    assert shein_costs(by_no, "19EXEMPLO1", "GSH19M00EXEMPL", ["19EXEMPLO1"]) == (Decimal("10.00"), Decimal("5.00"))


def test_shein_costs_wait_for_statement_and_partial_return():
    assert shein_costs(group_lines([_line("GSH1", 1, 100)]), "R1", "GSH1", ["R1"]) is None  # devolução sem lançamento
    assert shein_costs(group_lines([_line("R1", 2, 50)]), "R1", "GSH1", ["R1"]) is None  # receita fora da janela
    partial = group_lines([_line("GSH1", 1, 120), _line("R1", 2, 60)])
    assert shein_costs(partial, "R1", "GSH1", ["R1"]) == (Decimal("0.00"), Decimal("0.00"))


def test_confirm_receipt_sends_goods_ids():
    calls = []

    class FakeClient:
        def store_request(self, path, body, key, secret):
            calls.append((path, body))

    from app.models import Return, Store
    adapter = SheinAdapter(FakeClient())
    ret = Return(external_return_id="19EXMP0001", raw=DETAIL)
    adapter.confirm_receipt(Store(name="Shein", access_token="k", refresh_token="s"), ret)
    assert calls == [("/open-api/return-order/sign-return-order", {"returnOrderNo": "19EXMP0001", "goodsIdList": [4300000001]})]
