"""Dois avisos da mesma devolução nova chegando juntos não podem virar erro."""

import uuid
from types import SimpleNamespace

from sqlalchemy.exc import IntegrityError

from app.models import Store, WebhookEvent
from app.services import webhooks


class _RaceDB:
    def __init__(self, evt, store):
        self.evt, self.store = evt, store
        self.flushes = self.rollbacks = self.commits = 0

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, model, key):
        return self.evt if model is WebhookEvent else self.store

    def flush(self):
        self.flushes += 1
        if self.flushes == 1:  # o primeiro INSERT bate na devolução criada pelo outro aviso
            raise IntegrityError("INSERT INTO returns", {}, Exception("returns_store_id_external_return_id_key"))

    def rollback(self):
        self.rollbacks += 1

    def commit(self):
        self.commits += 1


def test_concurrent_insert_retries_as_update(monkeypatch):
    store = Store(id=uuid.uuid4(), platform="tiktok", name="Loja")
    evt = WebhookEvent(id=uuid.uuid4(), platform="tiktok", store_id=store.id, external_id="R1")
    db = _RaceDB(evt, store)
    calls = []
    monkeypatch.setattr(webhooks, "new_session", lambda: db)
    monkeypatch.setattr(webhooks, "get_adapter", lambda db, s: SimpleNamespace(fetch_return=lambda s, rid: {"id": rid}))
    monkeypatch.setattr(webhooks, "upsert_return", lambda db, s, nr, source: calls.append(nr))

    webhooks.process_event(evt.id)

    assert len(calls) == 2 and db.rollbacks == 1  # tentou criar, desfez e atualizou
    assert evt.error is None and evt.processed_at is not None and db.commits == 1
