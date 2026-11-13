import io
import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import Return, ReturnItem, User
from app.services import media
from app.services.users import (
    AuthError,
    can,
    hash_password,
    make_session,
    user_from_session,
    verify_password,
)
from app.ui.labels import money
from app.ui.routes import templates


class _DB:
    """Sessão falsa: devolve o usuário pelo id (para validar o cookie de sessão)."""

    def __init__(self, user=None):
        self.user = user

    def get(self, model, key):
        return self.user if self.user and key == self.user.id else None


def _user(role="operador", password="senha-forte-1"):
    return User(id=uuid.uuid4(), email="ana@loja.com", name="Ana", role=role,
                password_hash=hash_password(password), active=True)


# ---------- senha, sessão e permissões ----------

def test_password_hash_and_verify():
    h = hash_password("senha-forte-1")
    assert h.startswith("scrypt$") and "senha-forte-1" not in h
    assert verify_password("senha-forte-1", h)
    assert not verify_password("outra-senha", h)
    with pytest.raises(AuthError):
        hash_password("curta")


def test_session_roundtrip_and_password_change_invalidates():
    user = _user()
    token = make_session(user)
    assert user_from_session(_DB(user), token) is user
    assert user_from_session(_DB(user), token.replace(token[-4:], "0000")) is None
    user.password_hash = hash_password("nova-senha-123")
    assert user_from_session(_DB(user), token) is None  # trocar a senha derruba a sessão
    user.active = False
    assert user_from_session(_DB(user), make_session(user)) is None


def test_end_all_sessions_drops_old_ones():
    from app.services.users import end_all_sessions

    user = _user()
    user.session_version = 0
    old = make_session(user)
    db = _DB(user)
    db.commit = lambda: None
    end_all_sessions(db, user)
    assert user.session_version == 1
    assert user_from_session(db, old) is None  # aparelhos antigos saem
    assert user_from_session(db, make_session(user)) is user  # o deste aparelho é renovado


def test_reset_link_expires_and_works_once():
    import time

    from app.services.users import make_reset_token, user_from_reset_token

    user = _user()
    token = make_reset_token(user)
    assert user_from_reset_token(_DB(user), token) is user
    assert user_from_session(_DB(user), token) is None  # o link não serve como sessão
    assert user_from_reset_token(_DB(user), make_reset_token(user, now=time.time() - 31 * 60)) is None  # venceu
    user.password_hash = hash_password("nova-senha-123")
    assert user_from_reset_token(_DB(user), token) is None  # depois de trocar a senha, o link para de valer


def test_change_own_password_checks_current_and_repeat():
    from app.services.users import change_own_password

    user = _user()
    user.password_hash = hash_password("senha-atual-1")

    class DB:
        def commit(self):
            pass

    with pytest.raises(AuthError, match="atual"):
        change_own_password(DB(), user, "errada", "senha-nova-1", "senha-nova-1")
    with pytest.raises(AuthError, match="iguais"):
        change_own_password(DB(), user, "senha-atual-1", "senha-nova-1", "senha-nova-2")
    change_own_password(DB(), user, "senha-atual-1", "senha-nova-1", "senha-nova-1")
    assert verify_password("senha-nova-1", user.password_hash)


def test_permissions():
    op, gestor = _user("operador"), _user("gestor")
    assert can(op, "inspect") and can(op, "decide") and can(op, "erp")
    assert not can(op, "users")
    assert can(gestor, "users")
    assert not can(None, "inspect")


# ---------- armazenamento de mídia ----------

def test_local_storage_limits_and_path(tmp_path):
    store = media.LocalStorage(tmp_path)
    assert store.save("r1/a.jpg", io.BytesIO(b"x" * 10), max_bytes=100) == 10
    with pytest.raises(media.MediaError, match="limite"):
        store.save("r1/b.mp4", io.BytesIO(b"x" * 200), max_bytes=100)
    assert not (tmp_path / "r1" / "b.mp4").exists()  # arquivo parcial apagado
    with pytest.raises(media.MediaError):
        store.path("../fora.txt")


class _FakeBlob:
    def __init__(self, store, name):
        self.store, self.name = store, name

    def upload_from_file(self, f, size=None, content_type=None):
        self.store[self.name] = (f.read(), content_type)

    def download_as_bytes(self):
        from google.api_core.exceptions import NotFound

        if self.name not in self.store:
            raise NotFound("x")
        return self.store[self.name][0]

    def exists(self):
        return self.name in self.store

    def delete(self):
        self.store.pop(self.name, None)


class _FakeGCS:
    def __init__(self):
        self.objects = {}

    def bucket(self, name):
        self.bucket_name = name
        return SimpleNamespace(blob=lambda n: _FakeBlob(self.objects, n))


def test_gcs_storage_save_read_delete_and_limits():
    fake = _FakeGCS()
    store = media.GCSStorage("exemplo-midias", client=fake)
    assert store.save("r1/a.jpg", io.BytesIO(b"foto"), max_bytes=100, content_type="image/jpeg") == 4
    assert fake.objects["media/r1/a.jpg"] == (b"foto", "image/jpeg")
    assert store.read_bytes("r1/a.jpg") == b"foto"
    assert store.read_bytes("r1/nao-existe.jpg") is None
    with pytest.raises(media.MediaError, match="limite"):
        store.save("r1/b.mp4", io.BytesIO(b"x" * 200), max_bytes=100)
    assert "media/r1/b.mp4" not in fake.objects
    with pytest.raises(media.MediaError):
        store.save("../fora.jpg", io.BytesIO(b"x"), max_bytes=100)
    store.delete("r1/a.jpg")
    assert not fake.objects


def test_storage_backend_selection(monkeypatch):
    from app.config import get_settings

    monkeypatch.setattr(media, "get_settings", lambda: get_settings().model_copy(update={"media_backend": "local"}))
    assert isinstance(media.get_storage(), media.LocalStorage)
    monkeypatch.setattr(media, "get_settings",
                        lambda: get_settings().model_copy(update={"media_backend": "gcs", "gcs_bucket": ""}))
    with pytest.raises(media.MediaError, match="GCS_BUCKET"):
        media.get_storage()


def test_buyer_media_from_shopee_and_legacy():
    ret = Return(raw={"image": ["https://img/1.jpg"], "buyer_videos": [{"video_url": "https://v/1.mp4"}],
                      "legacy": {"fotos_cliente": ["https://img/1.jpg", "https://img/2.jpg"]}})
    kinds = [(m["kind"], m["url"]) for m in media.buyer_media(ret)]
    assert ("photo", "https://img/1.jpg") in kinds and ("video", "https://v/1.mp4") in kinds
    assert kinds.count(("photo", "https://img/1.jpg")) == 1


def test_evidence_photos_only_jpg_png_most_recent(tmp_path, monkeypatch):
    from app.models import ReturnMedia

    monkeypatch.setattr(media, "get_storage", lambda: media.LocalStorage(tmp_path))
    ret = Return(id=uuid.uuid4())
    ret.media = []
    now = datetime.now(UTC)
    for i, (kind, ctype) in enumerate([("photo", "image/jpeg"), ("video", "video/mp4"), ("photo", "image/heic"),
                                        ("photo", "image/png"), ("photo", "image/jpeg")]):
        path = f"{ret.id}/{i}.bin"
        media.LocalStorage(tmp_path).save(path, io.BytesIO(f"arq{i}".encode()), 100)
        ret.media.append(ReturnMedia(kind=kind, content_type=ctype, storage_path=path, original_name=f"{i}",
                                     size_bytes=4, created_at=now + timedelta(minutes=i)))
    photos = media.evidence_photos(ret, limit=2)
    assert [p[0] for p in photos] == ["4", "3"]  # mais recentes, sem vídeo nem HEIC


def test_report_periods():
    from datetime import date

    from app.ui.routes import period_range

    today = date(2026, 10, 16)
    assert period_range("mes", today) == (date(2026, 10, 1), today)
    assert period_range("mes_anterior", today) == (date(2026, 9, 1), date(2026, 9, 30))
    assert period_range("30d", today) == (date(2026, 9, 17), today)
    assert period_range("ano", today) == (date(2026, 1, 1), today)
    assert period_range("mes_anterior", date(2026, 1, 10)) == (date(2025, 12, 1), date(2025, 12, 31))


def test_reports_page_requires_gestor():
    assert not can(_user("operador"), "reports") and can(_user("gestor"), "reports")


def test_money_format():
    assert money(Decimal("1234.5")) == "R$ 1.234,50"


# ---------- rotas e templates ----------

@pytest.fixture
def client():
    app.dependency_overrides[get_db] = lambda: _DB()
    with TestClient(app) as c:
        yield c
    app.dependency_overrides.clear()


def test_queue_requires_login(client):
    r = client.get("/app", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].startswith("/app/login")


def test_login_page_and_static(client):
    r = client.get("/app/login")
    assert r.status_code == 200 and "Entrar" in r.text
    assert client.get("/static/app.css").status_code == 200
    assert client.get("/", follow_redirects=False).headers["location"] == "/app"


def _fake_return(**kw):
    ret = Return(
        id=uuid.uuid4(), platform="shopee", kind="customer_return", external_return_id="R1",
        external_order_id="P1", return_type="return_and_refund", status="received", reason_text="Não serviu",
        refund_amount=Decimal("79.90"), requested_at=datetime.now(UTC) - timedelta(days=2),
        action_deadline=datetime.now(UTC) - timedelta(hours=1), item_condition="resale",
        tracking_number="BR123", raw={"image": ["https://img/1.jpg"]}, **kw,
    )
    ret.store = SimpleNamespace(name="Loja Teste", label="Loja Teste")
    ret.items = [ReturnItem(product_name="Vestido", seller_sku="VEST-M", sku_name="M", quantity=1,
                            refund_amount=Decimal("79.90"))]
    ret.events, ret.media = [], []
    return ret


def _render(name, user, **ctx):
    base = {"user": user, "can": lambda p: can(user, p), "ok": None, "erro": None, "now": datetime.now(UTC)}
    return templates.env.get_template(name).render(**base, **ctx)


def test_detail_shows_inspection_and_decision_for_operator():
    ret = _fake_return()
    html = _render("detalhe.html", _user("operador"), ret=ret, physical=True, can_inspect=True, can_decide=True,
                   buyer_media=media.buyer_media(ret), invoice_mode="draft",
                   conditions={"resale": "Revenda", "defect": "Defeito"}, events=[])
    assert "Conferência do pacote" in html
    assert "Aceitar devolução" in html and "Recusar" in html
    assert "vencido" in html
    assert "Enviado pelo comprador" in html
    assert "VEST-M" in html


def test_detail_mismatch_option_and_contest_shortcut():
    ret = _fake_return()
    ctx = dict(physical=True, can_decide=True, buyer_media=[], invoice_mode="draft",
               conditions={"resale": "Revenda", "defect": "Defeito", "mismatch": "Item diferente"}, events=[])
    html = _render("detalhe.html", _user(), ret=ret, can_inspect=True, **ctx)
    assert "Item diferente do pedido" in html and "data-needs-photo" in html

    ret.received_at, ret.item_condition, ret.erp_stock_status = datetime.now(UTC), "mismatch", "skipped_mismatch"
    html = _render("detalhe.html", _user(), ret=ret, can_inspect=False, **ctx)
    assert "Veio outro produto no pacote" in html
    assert "recusar?motivo_interno=item_divergente" in html
    assert "NF de devolução é emitida normalmente" in html


def test_detail_shows_contest_for_shopee_accepted():
    ret = _fake_return()
    ret.status, ret.platform_status = "completed", "ACCEPTED"
    html = _render("detalhe.html", _user(), ret=ret, physical=True, can_inspect=False, can_decide=False,
                   can_contest=True, buyer_media=[], invoice_mode="draft", conditions={}, events=[])
    assert "Contestar devolução" in html and "Aceitar devolução" not in html


def test_contest_closed_explains_deadline_and_hides_button():
    ret = _fake_return()
    ret.status, ret.platform_status = "completed", "ACCEPTED"
    html = _render("detalhe.html", _user(), ret=ret, physical=True, can_inspect=False, can_decide=False, can_contest=False,
                   contest_closed=True, buyer_media=[], invoice_mode="draft", conditions={}, events=[])
    assert "Contestar devolução" not in html
    assert "não oferece mais contestação" in html and "o prazo de resposta acabou em" in html and "ela já foi aceita" in html


def test_reject_page_warns_resale_and_offers_way_out():
    ret = _fake_return()
    ret.item_condition, ret.condition_source, ret.decision = "resale", "manual", None
    reasons = [SimpleNamespace(code="used", label="Produto usado")]
    html = _render("recusar.html", _user(), ret=ret, reasons=reasons, internal=[], preselect="")
    assert "será aprovada automaticamente" in html
    assert "Nenhum motivo serve: voltar" in html and f'href="/app/devolucoes/{ret.id}"' in html

    ret.decision = "approved"  # já respondida: o aviso não vale mais
    assert "será aprovada automaticamente" not in _render("recusar.html", _user(), ret=ret, reasons=reasons, internal=[], preselect="")

    ret.decision, ret.condition_source = None, "auto"  # só sugestão pelo motivo: o app não aprova sozinho
    assert "será aprovada automaticamente" not in _render("recusar.html", _user(), ret=ret, reasons=reasons, internal=[], preselect="")


def test_contest_open_asks_platform_and_keeps_button_on_failure(monkeypatch):
    from app.ui import routes

    ret = _fake_return()
    monkeypatch.setattr(routes, "get_adapter", lambda db, store: SimpleNamespace(reject_reasons=lambda s, r: []))
    assert routes._contest_open(None, ret) is False
    monkeypatch.setattr(routes, "get_adapter", lambda db, store: SimpleNamespace(reject_reasons=lambda s, r: [object()]))
    assert routes._contest_open(None, ret) is True

    def broken(db, store):
        raise RuntimeError("Shopee fora do ar")

    monkeypatch.setattr(routes, "get_adapter", broken)
    assert routes._contest_open(None, ret) is True


def test_queue_template_renders_cards():
    ret = _fake_return()
    html = _render("fila.html", _user(), tabs={"decidir": "Aguardando decisão"}, aba="decidir", q="",
                   plataforma="", platforms={"shopee": "Shopee"}, rows=[ret], total=1, pagina=1, page_size=30,
                   counts={"decidir": 1, "conferir": 0, "vencidas": 1, "erros": 0})
    assert "Vestido" in html and "venceu" in html and "Pacote entregue" in html


def test_queue_shows_stopped_store_line_and_shift_pill():
    from app.services.store_status import StoreStatus

    shein = SimpleNamespace(platform="shein", label="Shein")
    tabs = {"decidir": "Aguardando decisão", "conferir": "Para conferir", "vencidas": "Vencidas", "erros": "Erros", "todas": "Todas"}
    html = _render("fila.html", _user(), tabs=tabs, aba="conferir", q="", plataforma="", platforms={}, rows=[], total=0,
                   pagina=1, page_size=30, counts={"decidir": 1, "conferir": 0, "vencidas": 0, "erros": 0},
                   store_alerts=[StoreStatus(shein, "bad", "parada há 3 dias", "chave inválida")],
                   shift=SimpleNamespace(state=None, worked_seconds=0))
    assert "Shein parada há 3 dias" in html and "Resolver" in html and "Shein · Shein" not in html
    assert "Iniciar turno" in html and "Registrar pacote não entregue" not in html


# ---------- painel de relatórios ----------

def test_report_params_parsing():
    from datetime import date

    from app.ui.routes import ReportParams

    p = ReportParams(inicio="2026-09-30", fim="2026-09-01", plataforma="shopee", loja="nao-e-uuid",
                     motivo="  Defeito ", sku="VEST-M", agrupar="ano")
    assert (p.start, p.end) == (date(2026, 9, 1), date(2026, 9, 30))  # datas invertidas são corrigidas
    assert p.platform == "shopee" and p.store_id is None and p.reason == "Defeito" and p.sku == "VEST-M"
    assert p.group == "day" and p.label() == "01/09/2026 a 30/09/2026"
    q = ReportParams(plataforma="amazon")
    assert q.platform is None and (q.end - q.start).days == 29  # padrão: últimos 30 dias


def test_dashboard_data_shape(monkeypatch):
    from datetime import date

    from app.routers import reports
    from app.services import dashboard

    monkeypatch.setattr(reports, "summary", lambda f, db: {"total": 10, "refunded_amount": 500.0, "avg_resolution_hours": 36})
    monkeypatch.setattr(reports, "costs", lambda f, group_by, db: {
        "totals": {"total": 120.0},
        "groups": [{"key": "Defeito", "returns": 3, "total": 40.0}, {"key": "Não serviu", "returns": 7, "total": 80.0}]
        if group_by == "reason" else [{"key": "VEST-M", "returns": 4, "total": 60.0}],
    })
    monkeypatch.setattr(reports, "by_condition", lambda f, db: [
        {"condition": "resale", "total": 5, "received": 5}, {"condition": "defect", "total": 2, "received": 2},
        {"condition": "mismatch", "total": 1, "received": 1}, {"condition": None, "total": 2, "received": 0}])
    monkeypatch.setattr(reports, "timeseries", lambda f, granularity, db: [{"period": "2026-10-02", "total": 4, "amount": 90.0}])
    monkeypatch.setattr(reports, "by_product", lambda f, limit, db: [
        {"seller_sku": "VEST-M", "product_name": "Vestido", "returns": 4, "units": 4, "defects": 1,
         "defect_rate": 0.25, "mismatches": 0}, {"seller_sku": None, "product_name": "?", "returns": 1, "units": 1,
         "defects": 0, "defect_rate": None, "mismatches": 0}])

    monkeypatch.setattr(dashboard, "product_extras", lambda db, f, skus: {
        "VEST-M": {"image": "https://img/v.jpg", "reasons": [{"reason": "Não serviu", "total": 3}]}})

    class DB:
        def execute(self, stmt):
            return SimpleNamespace(all=lambda: [("shopee", 6), ("tiktok", 4)])

        def scalars(self, stmt):
            return SimpleNamespace(all=lambda: [SimpleNamespace(id=uuid.uuid4(), name="Loja 1", label="Loja 1", platform="shopee")])

    d = dashboard.dashboard_data(DB(), date(2026, 10, 1), date(2026, 10, 3), reason="Defeito")
    assert d["previous"] == {"start": "2026-09-28", "end": "2026-09-30"}
    assert [s["total"] for s in d["series"]] == [0, 4, 0]  # dias vazios entram como zero
    cur = d["kpis"]["current"]
    assert cur["avg_days"] == 1.5 and cur["inspected"] == 8 and cur["defect_rate"] == 0.25 and cur["cost"] == 120.0
    assert d["reasons"][0]["reason"] == "Não serviu"  # ordenado por quantidade
    assert d["conditions"] == {"resale": 5, "defect": 2, "mismatch": 1, "not_inspected": 2}
    assert d["products"][0]["cost"] == 60.0 and d["products"][1]["sku"] == dashboard.NO_SKU
    assert d["products"][0]["image"] == "https://img/v.jpg" and d["products"][1]["reasons"] == []
    assert d["platforms"][0] == {"platform": "shopee", "total": 6} and d["filters"]["reason"] == "Defeito"
    assert len(dashboard.extra_conditions("Defeito", dashboard.NO_SKU)) == 2


def test_reports_template_renders_dashboard_shell():
    from app.ui.routes import PERIODS, ReportParams

    p = ReportParams(periodo="mes", motivo="Defeito")
    presets = {k: ["2026-10-01", "2026-10-16"] for k in PERIODS}
    html = _render("relatorios.html", _user("gestor"), p=p, periods=PERIODS, presets=presets,
                   platforms={"shopee": "Shopee", "tiktok": "TikTok"})
    assert 'data-reason="Defeito"' in html and "dashboard.js" in html and "page-wide" in html
    assert "Produtos que mais voltam" in html and 'data-preset="mes"' in html


def test_login_lockout_after_repeated_failures():
    from app.ui import routes

    routes._LOGIN_FAILS.clear()
    key = "alguem@exemplo.com"
    for _ in range(routes.LOGIN_MAX_FAILS - 1):
        routes._LOGIN_FAILS.setdefault(key, []).append(datetime.now(UTC))
    assert not routes._login_blocked(key)
    routes._LOGIN_FAILS[key].append(datetime.now(UTC))
    assert routes._login_blocked(key)
    routes._LOGIN_FAILS[key] = [datetime.now(UTC) - timedelta(minutes=routes.LOGIN_WINDOW_MIN + 1)] * 9
    assert not routes._login_blocked(key) and key not in routes._LOGIN_FAILS  # tentativas antigas expiram


def test_detail_has_camera_buttons():
    ret = _fake_return()
    ctx = dict(physical=True, can_decide=False, buyer_media=[], invoice_mode="draft",
               conditions={"resale": "Revenda", "defect": "Defeito", "mismatch": "Item diferente"}, events=[])
    html = _render("detalhe.html", _user(), ret=ret, can_inspect=True, **ctx)
    assert 'accept="image/*" capture="environment"' in html and 'accept="video/*" capture="environment"' in html
    assert "Tirar foto" in html and "Gravar vídeo" in html and "escolher da galeria" in html
    ret.received_at = datetime.now(UTC)  # depois da conferência: só adicionar mídias
    html = _render("detalhe.html", _user(), ret=ret, can_inspect=False, **ctx)
    assert "Adicionar fotos ou vídeos" in html and 'capture="environment"' in html


def test_search_handles_damaged_labels_and_invoice_key():
    from sqlalchemy.dialects import postgresql

    from app.ui.routes import _search_conditions

    def sql(q):
        return str(_search_conditions(q)[0].compile(dialect=postgresql.dialect(), compile_kwargs={"literal_binds": True}))

    full = sql("AP 1000-00002 br")  # leitor com espaços/traços
    assert "'AP100000002BR'" in full and "sale_tracking_number" in full and "CAST(returns.raw AS TEXT)" in full
    assert "LIKE '%%12398%%'" in sql("12398")  # parte legível da etiqueta rasurada
    assert "erp_sale_invoice_key = '" + "3" * 44 + "'" in sql("3" * 44)  # chave da NF (DANFE)
    assert "LIKE" not in sql("abc").replace("ILIKE", "")  # texto curto: só SKU/produto, sem trechos


def test_read_scan_understands_labels():
    from app.ui.routes import CEP_RE, GTIN_RE, read_scan

    assert read_scan('{"id":"40000000001","t":"lm"}') == "40000000001"  # QR code do Mercado Livre
    assert read_scan("14600000000001$000101001000") == "14600000000001"
    assert read_scan("  AP100000001BR ") == "AP100000001BR"
    assert read_scan("{nao é json") == "{nao é json"
    assert GTIN_RE.match("7891234567895") and not GTIN_RE.match("999882408839554")
    assert CEP_RE.match("01001000") and not CEP_RE.match("628954473")


def test_exact_search_is_the_first_try():
    from app.ui.routes import _search_conditions

    exact = str(_search_conditions("AP100000001BR", broad=False)[0])
    broad = str(_search_conditions("AP100000001BR")[0])
    assert "LIKE" not in exact.upper() and "LIKE" in broad.upper()


def test_envio_em_segundo_plano_responde_json(monkeypatch):
    """O vídeo sobe sozinho (app.js) e recebe o id em JSON; erro também volta em JSON."""
    import json as _json
    import uuid as _uuid
    from types import SimpleNamespace

    from app.ui import routes

    ret = SimpleNamespace(id=_uuid.uuid4())
    saved = SimpleNamespace(id=_uuid.uuid4())
    monkeypatch.setattr(routes, "_require", lambda user, perm: None)
    monkeypatch.setattr(routes, "_load", lambda db, rid: ret)
    monkeypatch.setattr(routes, "_save_uploads", lambda db, r, files, actor: [saved])
    user = SimpleNamespace(actor="Ana")
    req = SimpleNamespace(headers={"x-background-upload": "1"})
    resp = routes.upload_media(req, ret.id, [], user, None)
    assert _json.loads(resp.body) == {"ids": [str(saved.id)]}

    def boom(*a):
        raise routes.media.MediaError("Arquivo maior que o limite de 200 MB")

    monkeypatch.setattr(routes, "_save_uploads", boom)
    db = SimpleNamespace(rollback=lambda: None)
    resp = routes.upload_media(req, ret.id, [], user, db)
    assert resp.status_code == 400 and "200 MB" in _json.loads(resp.body)["erro"]

    resp = routes.upload_media(SimpleNamespace(headers={}), ret.id, [], user, db)
    assert resp.status_code == 303


def test_docs_require_login_and_security_headers(client):
    for path in ("/docs", "/openapi.json"):
        r = client.get(path, follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"].startswith("/app/login")
    assert client.get("/redoc").status_code == 404
    r = client.get("/app/login")
    assert r.headers["x-content-type-options"] == "nosniff" and r.headers["x-frame-options"] == "DENY"
    assert "strict-transport-security" not in r.headers
    r = client.get("/app/login", headers={"X-Forwarded-Proto": "https"})
    assert r.headers["strict-transport-security"].startswith("max-age=")


def test_reopened_post_tab_goes_back_to_page(client):
    # Chrome reabre a aba de um envio interrompido com GET: volta para a devolução, sem {"detail": ...}
    rid = "ef8a3796-721f-40e9-9560-bee40e13c837"
    r = client.get(f"/app/devolucoes/{rid}/conferir", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == f"/app/devolucoes/{rid}"
    r = client.get("/app/custos/novo", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/app"
    assert client.get("/integracao/v1/nao-existe").status_code == 404


def test_svg_upload_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_storage", lambda: media.LocalStorage(tmp_path))
    ret = _fake_return()
    with pytest.raises(media.MediaError):
        media.save_media(None, ret, io.BytesIO(b"<svg onload=alert(1)>"), "x.svg", "image/svg+xml", "Ana")


def test_media_folder_is_platform_and_order(tmp_path, monkeypatch):
    monkeypatch.setattr(media, "get_storage", lambda: media.LocalStorage(tmp_path))
    ret = _fake_return()
    ret.external_order_id = "2609 10EX/EMPL0W"
    db = SimpleNamespace(add=lambda _: None, commit=lambda: None)
    m = media.save_media(db, ret, io.BytesIO(b"jpg"), "foto da peça.jpg", "image/jpeg", "Ana")
    assert m.storage_path == f"shopee/2609_10EX_EMPL0W/{m.id.hex[:8]}_foto_da_pe_a.jpg"
    assert (tmp_path / m.storage_path).read_bytes() == b"jpg"
    ret.external_order_id = None  # sem pedido: usa o número da devolução
    assert media.return_folder(ret) == "shopee/R1"
