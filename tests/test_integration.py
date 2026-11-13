"""Chaves de integração (somente leitura) e a API /integracao/v1."""

import uuid
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient

from app.db import get_db
from app.main import app
from app.models import ApiKey, Return, ReturnItem
from app.routers import integration
from app.services import api_keys


class _KeysDB:
    """Sessão falsa só com a tabela de chaves."""

    def __init__(self):
        self.keys: list[ApiKey] = []
        self.commits = 0

    def add(self, obj):
        obj.id = obj.id or uuid.uuid4()
        self.keys.append(obj)

    def commit(self):
        self.commits += 1

    def get(self, model, key_id):
        return next((k for k in self.keys if k.id == key_id), None)

    def scalar(self, stmt):
        # where key_hash == ? and revoked_at is null
        wanted = stmt.whereclause.clauses[0].right.value
        return next((k for k in self.keys if k.key_hash == wanted and k.revoked_at is None), None)


def test_create_verify_and_revoke_key():
    db = _KeysDB()
    key, token = api_keys.create_key(db, "  Sistema financeiro ", created_by="gestor@loja.com")
    assert token.startswith("pdv_") and len(token) > 40
    assert key.name == "Sistema financeiro" and key.prefix == token[:12]
    assert key.key_hash != token and token not in key.key_hash  # só o hash fica guardado

    assert api_keys.verify_key(db, token) is key and key.last_used_at is not None
    assert api_keys.verify_key(db, token + "x") is None
    assert api_keys.verify_key(db, "outra-coisa") is None and api_keys.verify_key(db, None) is None

    api_keys.revoke_key(db, key.id)
    assert key.revoked_at is not None and api_keys.verify_key(db, token) is None


def test_create_key_requires_name():
    with pytest.raises(ValueError):
        api_keys.create_key(_KeysDB(), "   ")


def test_last_used_is_throttled():
    db = _KeysDB()
    key, token = api_keys.create_key(db, "BI")
    api_keys.verify_key(db, token)
    commits = db.commits
    api_keys.verify_key(db, token)
    assert db.commits == commits  # dentro de 5 min não grava de novo
    key.last_used_at = datetime.now(UTC) - timedelta(minutes=6)
    api_keys.verify_key(db, token)
    assert db.commits == commits + 1


@pytest.fixture
def client():
    db = _KeysDB()
    app.dependency_overrides[get_db] = lambda: db
    with TestClient(app) as c:
        c.keys_db = db
        yield c
    app.dependency_overrides.clear()


def test_integration_requires_valid_key(client):
    assert client.get("/integracao/v1/devolucoes").status_code == 401
    assert client.get("/integracao/v1/painel", headers={"X-API-Key": "pdv_errada"}).status_code == 401
    # A chave principal (X-API-Key do .env) não serve aqui, e a de integração não serve na API principal
    _, token = api_keys.create_key(client.keys_db, "ERP")
    assert client.get("/returns", headers={"X-API-Key": token}).status_code == 401


def test_integration_validates_filters(client, monkeypatch):
    _, token = api_keys.create_key(client.keys_db, "ERP")
    h = {"X-API-Key": token}
    monkeypatch.setattr(integration.dashboard, "dashboard_data", lambda *a, **k: {"ok": True})
    assert client.get("/integracao/v1/painel", headers=h).json() == {"ok": True}
    assert client.get("/integracao/v1/painel?plataforma=amazon", headers=h).status_code == 422
    assert client.get("/integracao/v1/painel?inicio=2026-10-10&fim=2026-10-01", headers=h).status_code == 422
    assert client.get("/integracao/v1/custos?agrupar_por=cliente", headers=h).status_code == 422


def test_return_row_has_costs_and_no_buyer_data():
    ret = Return(
        id=uuid.uuid4(), store_id=uuid.uuid4(), platform="shopee", kind="customer_return", external_return_id="R1",
        external_order_id="P1", return_type="return_and_refund", status="completed", reason_text="Não serviu",
        refund_amount=Decimal("79.90"), requested_at=datetime(2026, 10, 2, 12, tzinfo=UTC), item_condition="defect",
        cost_return_shipping=Decimal("0"), cost_product_loss=Decimal("35.50"), cost_operational=Decimal("2"),
        cost_total=Decimal("37.50"), liability="buyer", internal_notes="não sair daqui", raw={"buyer": "x"},
    )
    ret.store = SimpleNamespace(name="Loja 1", label="Loja 1")
    ret.items = [ReturnItem(seller_sku="VEST-M", product_name="Vestido", sku_name="M", quantity=2,
                            refund_amount=Decimal("79.90"))]
    row = integration._return_row(ret)
    assert row["plataforma_nome"] == "Shopee" and row["loja"]["nome"] == "Loja 1"
    assert row["custos"] == {"frete_devolucao": 0.0, "perda_produto": 35.5, "operacional": 2.0, "venda_perdida": 0.0,
                             "confirmado_extrato": False, "total": 37.5,
                             "responsavel": "buyer"}
    assert row["itens"][0]["quantidade"] == 2 and row["condicao"] == "defect"
    text = str(row)
    assert "não sair daqui" not in text and "buyer': 'x" not in text  # nada de notas internas nem raw


def test_keys_page_shows_new_key_once():
    from app.models import User
    from app.services.users import can
    from app.ui.routes import templates

    user = User(id=uuid.uuid4(), email="g@loja.com", name="G", role="gestor", password_hash="x", active=True)
    keys = [ApiKey(id=uuid.uuid4(), name="ERP", prefix="pdv_AbCdEfGh", key_hash="h",
                   created_at=datetime.now(UTC), revoked_at=None)]
    ctx = {"user": user, "can": lambda p: can(user, p), "ok": None, "erro": None, "now": datetime.now(UTC),
           "keys": keys, "api_base": "https://x/integracao/v1"}
    html = templates.env.get_template("integracoes.html").render(**ctx, new_key="pdv_SEGREDO", new_key_name="ERP")
    from app.services.media_backup import BackupStatus

    backup = BackupStatus(True, False, total=160, copied=150, waiting=3, used_bytes=50 * 1024**2, limit_bytes=966367641,
                          last=datetime.now(UTC))
    panel = templates.env.get_template("integracoes.html").render(**ctx, backup=backup)
    assert "150 de 157" in panel and "3 vídeo(s) esperando o plano pago" in panel and "0,05 GB" in panel
    assert "pdv_SEGREDO" in html and "não aparece de novo" in html and "pdv_AbCdEfGh" in html
    assert "pdv_SEGREDO" not in templates.env.get_template("integracoes.html").render(**ctx)
