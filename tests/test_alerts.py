import uuid
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

from app.config import get_settings
from app.models import Return, ReturnItem
from app.services import alerts


def _settings(**kw):
    base = dict(smtp_host="smtp.test", smtp_user="avisos@loja.com", smtp_password="x", alert_emails="a@loja.com, b@loja.com",
                alert_digest_hours="8,14", alert_urgent_hours=12, app_url="http://192.168.0.20:8000",
                timezone="America/Sao_Paulo")
    return get_settings().model_copy(update={**base, **kw})


def _ret(hours_left, platform="shopee"):
    now = datetime.now(UTC)
    r = Return(id=uuid.uuid4(), platform=platform, status="pending_review", external_order_id="P1",
               action_deadline=now + timedelta(hours=hours_left))
    r.items = [ReturnItem(product_name="Vestido Linho")]
    r.store = SimpleNamespace(name="Loja Exemplo", label="Loja Exemplo")
    return r


class _DB:
    def __init__(self, soon=()):
        self.sent, self.soon = {}, list(soon)

    def get(self, model, key):
        return self.sent.get(key)

    def add(self, obj):
        self.sent[obj.key] = obj

    def commit(self):
        pass

    def scalars(self, stmt):
        return SimpleNamespace(all=lambda: self.soon)


def test_digest_key_follows_local_schedule():
    s = _settings()
    # 10:30 UTC = 07:30 em São Paulo: antes do primeiro resumo
    assert alerts.due_digest_key(s, datetime(2026, 10, 16, 10, 30, tzinfo=UTC)) is None
    assert alerts.due_digest_key(s, datetime(2026, 10, 16, 11, 5, tzinfo=UTC)) == "digest:2026-10-16:08"
    assert alerts.due_digest_key(s, datetime(2026, 10, 16, 20, 0, tzinfo=UTC)) == "digest:2026-10-16:14"


def test_not_configured_is_skipped():
    assert "skipped" in alerts.run_alerts(_DB(), settings=_settings(smtp_password=""))


def test_digest_once_and_urgent_once(monkeypatch):
    sent = []
    monkeypatch.setattr(alerts, "send_email", lambda s, msg, to: sent.append((msg.subject, to)))
    monkeypatch.setattr(alerts, "build_digest", lambda db, now: alerts.Digest(overdue=[_ret(-3)], waiting=5))
    db, s = _DB(soon=[_ret(5)]), _settings()
    now = datetime(2026, 10, 16, 11, 5, tzinfo=UTC)

    first = alerts.run_alerts(db, now=now, settings=s)
    assert first["digest"].startswith("Devoluções 16/10 08h: 1 vencida")
    assert first["urgent"] == 1
    assert sent[0][1] == ["a@loja.com", "b@loja.com"]

    again = alerts.run_alerts(db, now=now + timedelta(minutes=15), settings=s)
    assert again == {"digest": None, "urgent": 0}  # nada repetido
    assert len(sent) == 2


def test_messages_have_links_and_text_version():
    s = _settings()
    d = alerts.Digest(overdue=[_ret(-2)], due_soon=[_ret(6)], waiting=3, delivered_to_inspect=7, erp_errors=1)
    msg = alerts.digest_message(d, s, datetime.now(UTC))
    assert "http://192.168.0.20:8000/app/devolucoes/" in msg.html
    assert "PRAZO VENCIDO" in msg.text and "Pacotes entregues para conferir: 7" in msg.text
    urgent = alerts.urgent_message([_ret(3)], s)
    assert "menos de 12h" in urgent.subject and "Vestido Linho" in urgent.text
