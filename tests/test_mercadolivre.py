from decimal import Decimal
from urllib.parse import parse_qs, urlparse

import httpx
import pytest

from app.enums import ReturnStatus, ReturnType
from app.models import Return
from app.platforms.base import PlatformError
from app.platforms.mercadolivre.adapter import MercadoLivreAdapter
from app.platforms.mercadolivre.client import MercadoLivreClient, pkce_challenge
from app.platforms.mercadolivre.mapper import map_claim, map_status
from app.services.erp import _ecommerce_numbers, classify

SELLER = {"role": "respondent", "type": "seller", "user_id": 1, "available_actions": []}
CLAIM = {
    "id": 5500000003,
    "resource_id": 2000010000000001,
    "status": "closed",
    "type": "returns",
    "stage": "claim",
    "resource": "order",
    "reason_id": "PDD9939",
    "players": [{"role": "complainant", "type": "buyer", "user_id": 2, "available_actions": []}, SELLER],
    "resolution": {"reason": "item_returned", "benefited": ["complainant"]},
    "date_created": "2026-07-03T09:34:02.000-04:00",
    "last_updated": "2026-07-23T07:54:49.000-04:00",
}
RETURN = {
    "status": "delivered",
    "shipments": [{"status": "delivered", "tracking_number": "AP100000003BR", "type": "return"}],
    "orders": [{"order_id": 2000010000000001, "item_id": "MLB1", "variation_id": 100000000001, "return_quantity": "1.0"}],
}
ORDER = {
    "id": 2000010000000001,
    "pack_id": 2000010000000002,
    "total_amount": 197.98,
    "currency_id": "BRL",
    "buyer": {"nickname": "nao-deve-ser-gravado"},
    "order_items": [
        {"item": {"id": "MLB1", "title": "Vestido Linho", "seller_sku": None, "variation_id": 100000000001,
                  "variation_attributes": [{"value_name": "M"}, {"value_name": "Cru"}]},
         "quantity": 1, "unit_price": 98.99},
        {"item": {"id": "MLB2", "title": "Outro", "seller_sku": "OUTRO-P", "variation_id": 5}, "quantity": 1,
         "unit_price": 99.0},
    ],
}
REASON = {"id": "PDD9939", "name": "repentant_buyer", "detail": "A minha compra chegou em boas condições"}


def test_map_claim_uses_returned_items_and_sku_fallback():
    nr = map_claim(CLAIM, RETURN, ORDER, REASON, sku_lookup=lambda item, var: "VM-CRU-M")
    assert nr.external_return_id == "5500000003"
    assert nr.external_order_id == "2000010000000001"
    assert nr.status == ReturnStatus.COMPLETED
    assert nr.return_type == ReturnType.RETURN_AND_REFUND
    assert nr.reason_text == "Arrependimento"
    assert nr.tracking_number == "AP100000003BR"
    assert len(nr.items) == 1  # só o item devolvido, não o pedido inteiro
    assert nr.items[0].seller_sku == "VM-CRU-M"
    assert nr.items[0].sku_name == "M / Cru"
    assert nr.refund_amount == Decimal("98.99")
    assert nr.platform_status == "returns:claim:closed / return:delivered"
    assert nr.raw["order"]["pack_id"] == 2000010000000002
    assert "buyer" not in nr.raw["order"]


def test_without_return_is_refund_only_whole_order():
    claim = {**CLAIM, "type": "mediations", "reason_id": "PNR9567"}
    nr = map_claim(claim, None, ORDER, {"name": "delivered_but_not_receive_package"})
    assert nr.return_type == ReturnType.REFUND_ONLY
    assert len(nr.items) == 2
    assert nr.items[1].seller_sku == "OUTRO-P"
    assert nr.reason_text == "Não recebi o pacote"


def _open(actions=(), stage="claim"):
    seller = {**SELLER, "available_actions": [{"action": a, "due_date": None} for a in actions]}
    return {**CLAIM, "status": "opened", "stage": stage, "resolution": None, "players": [seller]}


@pytest.mark.parametrize(
    "claim,ret,expected",
    [
        (CLAIM, RETURN, ReturnStatus.COMPLETED),
        ({**CLAIM, "resolution": {"benefited": ["respondent"]}}, None, ReturnStatus.REJECTED),
        ({**CLAIM, "resolution": {"benefited": []}}, None, ReturnStatus.CANCELLED),
        (_open(["refund"], stage="dispute"), None, ReturnStatus.DISPUTED),
        (_open(["refund", "open_dispute"]), {"status": "label_generated"}, ReturnStatus.AWAITING_BUYER_SHIPMENT),
        (_open(["refund"]), {"status": "shipped"}, ReturnStatus.IN_TRANSIT),
        (_open(["return_review_ok"]), {"status": "delivered"}, ReturnStatus.RECEIVED),
        (_open(["refund", "open_dispute"]), None, ReturnStatus.PENDING_REVIEW),
        (_open([]), None, ReturnStatus.DISPUTED),
        # Reembolsada e fechada, mas a revisão da peça ("Já revisei") ainda está aberta para a loja
        ({**_open(["return_review_ok", "return_review_fail"]), "status": "closed",
          "resolution": {"benefited": ["complainant"]}}, {"status": "delivered"}, ReturnStatus.RECEIVED),
        ({**_open(["return_review_ok"]), "status": "closed"}, {"status": "shipped"}, ReturnStatus.IN_TRANSIT),
    ],
)
def test_map_status(claim, ret, expected):
    assert map_status(claim, ret) == expected


RULES = [("defeito", "defect", 10), ("grande", "resale", 50), ("pequeno", "resale", 50), ("cor", "resale", 50),
         ("errado", "resale", 60), ("arrependimento", "resale", 60)]


@pytest.mark.parametrize(
    "name,expected",
    [("repentant_buyer", "resale"), ("broken_item_fashion", "defect"), ("bigger_than_expected_fashion", "resale"),
     ("smaller_than_expected_fashion", "resale"), ("different_color_or_size_fashion", "resale"),
     ("different_item_other", "resale")],
)
def test_reason_labels_feed_classification(name, expected):
    nr = map_claim(CLAIM, RETURN, ORDER, {"name": name})
    assert classify(nr.reason_code, nr.reason_text, RULES) == expected


def test_olist_lookup_also_tries_pack_id():
    ret = Return(external_order_id="2000010000000001", raw={"order": {"pack_id": 2000010000000002}})
    assert _ecommerce_numbers(ret) == ["2000010000000001", "2000010000000002"]


def _client(handler):
    return MercadoLivreClient("cid", "secret", "https://api.test", "https://auth.test/authorization",
                              http=httpx.Client(transport=httpx.MockTransport(handler)), max_retries=1,
                              min_interval=0)


def test_authorize_url_uses_pkce():
    url = _client(lambda r: None).authorize_url("https://x/", "st", "verifier123")
    q = {k: v[0] for k, v in parse_qs(urlparse(url).query).items()}
    assert q["code_challenge"] == pkce_challenge("verifier123") and q["code_challenge_method"] == "S256"
    assert q["redirect_uri"] == "https://x/" and q["state"] == "st"


def test_iter_claims_stops_at_cutoff():
    from datetime import datetime

    def handler(req: httpx.Request):
        assert req.headers["authorization"] == "Bearer tok"
        assert req.url.params["sort"] == "last_updated:desc"
        return httpx.Response(200, json={"paging": {"total": 3}, "data": [
            {"id": 1, "last_updated": "2026-10-10T10:00:00.000-03:00"},
            {"id": 2, "last_updated": "2026-10-05T10:00:00.000-03:00"},
            {"id": 3, "last_updated": "2026-09-01T10:00:00.000-03:00"},
        ]})

    since = datetime.fromisoformat("2026-10-01T00:00:00-03:00")
    assert [c["id"] for c in _client(handler).iter_claims("tok", "1", since)] == [1, 2]


def test_missing_return_is_none_and_errors_raise():
    def handler(req: httpx.Request):
        if req.url.path.endswith("/returns"):
            return httpx.Response(404, json={"error": "not_found_error", "message": "no return"})
        return httpx.Response(403, json={"error": "forbidden", "message": "scope"})

    c = _client(handler)
    assert c.get_claim_return("tok", "1") is None
    with pytest.raises(PlatformError, match="403"):
        c.get_order("tok", "1")


class _Store:
    name, external_shop_id = "Loja ML", "1"


def _action_adapter(actions, calls, photos=()):
    """Adaptador com API falsa: a reclamação tem `actions` disponíveis; registra os POSTs."""

    def handler(req: httpx.Request):
        path = req.url.path
        if req.method == "POST" and path.endswith("/returns/attachments"):
            calls.append((path, "upload"))
            return httpx.Response(200, json={"user_id": 1, "file_name": f"anexo{len(calls)}.jpg"})
        if req.method == "POST":
            calls.append((path, req.content.decode()))
            return httpx.Response(200, json={})
        if path.endswith("/returns/reasons"):
            return httpx.Response(200, json=[{"id": "SRF2", "detail": "O produto chegou avariado"},
                                             {"id": "SRF3", "detail": "A devolução está incompleta"}])
        if path.endswith("/returns"):
            return httpx.Response(200, json={"id": 100000001})
        return httpx.Response(200, json=_open(actions))

    return MercadoLivreAdapter(_client(handler), lambda s: "tok", evidence_provider=lambda ret: list(photos))


def _ret():
    return Return(external_return_id="5500000001", raw={"return": {"id": 100000001}})


def test_approve_received_return_does_review_ok():
    calls = []
    _action_adapter(["return_review_ok", "return_review_fail", "refund", "open_dispute"], calls).approve(_Store(), _ret())
    assert calls == [("/post-purchase/v1/returns/100000001/return-review", "[]")]  # o ML recusa {} na revisão OK


def test_approve_without_package_refunds():
    calls = []
    _action_adapter(["refund", "open_dispute"], calls).approve(_Store(), _ret())
    assert calls[0][0] == "/marketplace/v2/claims/5500000001/expected-resolutions/refund"


def test_approve_can_force_allow_return_and_rejects_unavailable():
    calls = []
    adapter = _action_adapter(["refund", "allow_return"], calls)
    adapter.approve(_Store(), _ret(), decision="allow_return")
    assert calls[0][0].endswith("/expected-resolutions/allow-return")
    with pytest.raises(PlatformError, match="Nenhuma ação"):
        _action_adapter([], []).approve(_Store(), _ret())


def test_reject_reasons_and_review_fail():
    calls = []
    adapter = _action_adapter(["return_review_ok", "return_review_fail", "open_dispute"], calls)
    reasons = {r.code: r.label for r in adapter.reject_reasons(_Store(), _ret())}
    assert set(reasons) == {"SRF2", "SRF3", "open_dispute"}
    assert "envia as fotos" in reasons["SRF2"]

    adapter.reject(_Store(), _ret(), "SRF3", "Faltou o cinto")  # sem fotos: permitido fora do SRF2/SRF4
    assert calls[-1][0] == "/post-purchase/v1/returns/100000001/return-review"
    assert '"reason":"SRF3"' in calls[-1][1].replace(" ", "") and "attachments" not in calls[-1][1]

    with pytest.raises(PlatformError, match="exige fotos"):
        adapter.reject(_Store(), _ret(), "SRF2", "Avariado")
    with pytest.raises(PlatformError, match="comentário"):
        adapter.reject(_Store(), _ret(), "SRF3", None)


def test_review_fail_uploads_photos_as_attachments():
    calls = []
    photos = [(f"f{i}.jpg", b"jpeg", "image/jpeg") for i in range(5)]
    adapter = _action_adapter(["return_review_fail", "open_dispute"], calls, photos=photos)
    adapter.reject(_Store(), _ret(), "SRF4", "Veio um chinelo no lugar do vestido")
    uploads = [c for c in calls if c[1] == "upload"]
    assert len(uploads) == 3 and uploads[0][0] == "/post-purchase/v1/claims/5500000001/returns/attachments"
    import json as _json
    review = _json.loads(calls[-1][1])[0]
    assert review["reason"] == "SRF4" and review["attachments"] == ["anexo1.jpg", "anexo2.jpg", "anexo3.jpg"]


def test_reject_open_dispute():
    calls = []
    _action_adapter(["refund", "open_dispute"], calls).reject(_Store(), _ret(), "open_dispute", None)
    assert calls[0][0] == "/marketplace/v2/claims/5500000001/actions/open-dispute"


def test_return_cost_feeds_company_cost():
    from app.services.costs import compute_costs

    nr = map_claim(_open(["refund"]), {"status": "shipped"}, ORDER, REASON, return_cost={"amount": 23.5})
    ret = Return(platform="mercado_livre", kind="customer_return", status=nr.status, return_type=nr.return_type,
                 item_condition="resale", raw=nr.raw)
    ret.items = []
    compute_costs(ret)
    assert ret.cost_return_shipping == Decimal("23.50")
    assert ret.liability == "seller"


def test_webhook_claim_id_from_resource():
    from app.services.webhooks import mercadolivre_claim_id

    assert mercadolivre_claim_id("/post-purchase/v1/claims/5500000002") == "5500000002"
    assert mercadolivre_claim_id("/v1/claims/123/actions") == "123"
    assert mercadolivre_claim_id("/orders/1") is None


def test_exchange_numbers_are_kept_for_search():
    # Caso real: troca do pedido 2000010000000004; o ML mostra a revisão no pacote novo 2000010000000005
    def handler(req: httpx.Request):
        path = req.url.path
        if path.endswith("/returns"):
            return httpx.Response(404, json={"error": "not_found_error", "message": "no return"})
        if path.endswith("/changes"):
            return httpx.Response(200, json={"data": [{
                "status": "change_return_delivered", "new_orders_ids": [{"id": 2000010000000003}],
                "new_orders_shipments": [{"id": 40000000002}]}]})
        if path.endswith("/orders/2000010000000003"):
            return httpx.Response(200, json={"id": 2000010000000003, "pack_id": 2000010000000005})
        if "/orders/" in path:
            return httpx.Response(200, json={"id": 2000010000000004, "order_items": []})
        return httpx.Response(200, json={"id": "PDD9939", "name": "x"})

    adapter = MercadoLivreAdapter(_client(handler), lambda s: "tok")
    claim = {"id": 5500000004, "type": "mediations", "stage": "dispute", "status": "closed", "resource": "order",
             "resource_id": 2000010000000004, "reason_id": "PDD9939", "related_entities": ["return", "change"],
             "resolution": {"reason": "item_changed"}}
    nr = adapter._build("tok", claim)
    assert nr.raw["change"] == {"orders": ["2000010000000003"], "packs": ["2000010000000005"],
                                "shipments": ["40000000002"], "status": "change_return_delivered"}


@pytest.mark.parametrize("claim,expected", [
    ({"related_entities": ["return", "change"]}, True),
    ({"status": "closed", "resolution": {"reason": "item_changed"}}, True),  # vindo da busca, troca resolvida
    ({"status": "opened"}, True),                                            # vindo da busca, ainda aberta
    ({"status": "closed", "resolution": {"reason": "payment_refunded"}}, False),
    ({"status": "opened", "related_entities": ["return"]}, False),
])
def test_when_to_look_for_exchange(claim, expected):
    from app.platforms.mercadolivre.adapter import _may_have_change

    assert _may_have_change(claim) is expected
