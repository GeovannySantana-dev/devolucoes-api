import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.config import get_settings
from app.models import AlertSent, Store
from app.services import health


class _DB:
    def __init__(self, stores):
        self.stores, self.sent = stores, {}

    def scalars(self, stmt):
        if stmt.column_descriptions[0]["entity"] is AlertSent:  # avisos abertos de loja parada
            return SimpleNamespace(all=lambda: [a for k, a in self.sent.items() if k.startswith("open:store:")])
        return SimpleNamespace(all=lambda: self.stores)

    def scalar(self, stmt):
        return 7  # devoluções novas desde o aviso

    def get(self, model, key):
        if model is Store:
            return next((s for s in self.stores if s.id == key), None)
        return self.sent.get(key)

    def add(self, obj):
        obj.sent_at = obj.sent_at or NOW
        self.sent[obj.key] = obj

    def delete(self, obj):
        self.sent.pop(obj.key)

    def commit(self):
        pass


NOW = datetime(2026, 10, 16, 15, 0, tzinfo=UTC)


def _store(**kw):
    base = dict(id=uuid.uuid4(), platform="shopee", name="Loja Exemplo", active=True, access_token="t",
                last_synced_at=NOW - timedelta(minutes=10), refresh_token_expires_at=NOW + timedelta(days=20))
    return Store(**{**base, **kw})


def test_detects_failed_step_stopped_store_with_reason_and_expiring_token():
    results = {"olist_link": {"ok": False, "error": "Olist HTTP 503"}}
    stores = [_store(), _store(name="Parada", last_synced_at=NOW - timedelta(hours=5),
                               last_sync_error="Shopee: token inválido [error_auth: x] (/api/v2)"),
              _store(name="Recente", last_synced_at=NOW - timedelta(minutes=50), last_sync_error="falhou 1 vez"),
              _store(name="Vencendo", refresh_token_expires_at=NOW + timedelta(days=1))]
    problems = dict(health.find_problems(_DB(stores), results, NOW))
    text = " | ".join(problems.values())
    assert "olist_link" in text and "Vencendo" in text
    assert "Parada: parada desde" in text and "Motivo: token inválido" in text and "error_auth" not in text
    assert "Recente" not in text  # falha de menos de 2 h ainda não é loja parada
    assert len(problems) == 3


def _settings():
    return get_settings().model_copy(update=dict(smtp_host="smtp", smtp_user="u", smtp_password="p",
                                                 admin_emails="gestor@loja.com", alert_emails=""))


def test_stopped_store_emailed_once_and_again_when_it_comes_back(monkeypatch):
    sent = []
    monkeypatch.setattr(health.alerts, "send_email", lambda s, msg, to: sent.append((msg.subject, to)))
    s = _settings()
    store = _store(platform="shein", name="Shein", last_synced_at=NOW - timedelta(hours=5))
    db = _DB([store])
    assert health.check_health(db, {}, now=NOW, settings=s) == {"problems": 1, "new": 1}
    assert sent == [("⛔ Shein parada: devoluções novas não estão entrando", ["gestor@loja.com"])]
    # no dia seguinte continua parada: não repete
    assert health.check_health(db, {}, now=NOW + timedelta(days=1), settings=s)["new"] == 0
    assert len(sent) == 1
    store.last_synced_at = NOW + timedelta(days=2)
    assert health.check_health(db, {}, now=NOW + timedelta(days=2, minutes=5), settings=s) == {"problems": 0, "resolved": 1}
    assert sent[-1][0] == "✅ Shein voltou a sincronizar"
    assert not any(k.startswith("open:") for k in db.sent)


def test_other_problems_emailed_once_per_day(monkeypatch):
    sent = []
    monkeypatch.setattr(health.alerts, "send_email", lambda s, msg, to: sent.append(msg.subject))
    db = _DB([])
    results = {"olist_link": {"ok": False, "error": "Olist HTTP 503"}}
    assert health.check_health(db, results, now=NOW, settings=_settings())["new"] == 1
    assert health.check_health(db, results, now=NOW + timedelta(minutes=15), settings=_settings())["new"] == 0
    assert health.check_health(db, results, now=NOW + timedelta(days=1), settings=_settings())["new"] == 1
    assert sent == ["⚠ Devoluções: 1 problema(s) nas integrações"] * 2


def test_no_email_configured_only_reports():
    s = get_settings().model_copy(update=dict(smtp_password="", admin_emails="", alert_emails=""))
    db = _DB([_store(name="Parada", last_synced_at=NOW - timedelta(hours=5))])
    assert health.check_health(db, {}, now=NOW, settings=s)["skipped"] == "e-mail não configurado"


def test_public_ip_change_is_reported():
    db = _DB([])
    s = get_settings()
    assert health.check_public_ip(db, s, fetch=lambda: "187.89.138.104") == {"ip": "187.89.138.104"}  # 1ª vez só guarda
    assert health.check_public_ip(db, s, fetch=lambda: "187.89.138.104\n") == {"ip": "187.89.138.104"}
    out = health.check_public_ip(db, s, fetch=lambda: "177.10.20.30")
    assert out == {"ip": "177.10.20.30", "changed_from": "187.89.138.104"}
    problems = health.find_problems(db, {"ip": {"ok": True, "result": out}}, NOW)
    assert any(k == "ip:177.10.20.30" and "lista branca" in d for k, d in problems)


def test_store_status_levels_and_reason():
    from app.services import store_status

    ok = store_status.status(_store(), NOW)
    assert (ok.level, ok.label) == ("ok", "sincronizando")
    stopped = store_status.status(_store(last_synced_at=NOW - timedelta(days=3), sync_error_since=NOW - timedelta(days=2, hours=20),
                                         last_sync_error="Shein: chave da loja inválida [openapi00001: x] (/open-api/a)"), NOW)
    assert (stopped.level, stopped.label, stopped.detail) == ("bad", "parada há 2 dias", "chave da loja inválida")
    expiring = store_status.status(_store(refresh_token_expires_at=NOW + timedelta(days=5, hours=1)), NOW)
    assert (expiring.level, expiring.label) == ("warn", "vence em 5 dias")
    assert store_status.status(_store(active=False), NOW).level == "off"
