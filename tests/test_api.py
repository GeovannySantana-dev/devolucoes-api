"""Testes de rota sem banco: as queries são compiladas para PostgreSQL para validar o SQL gerado."""

from collections import namedtuple

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.dialects import postgresql

from app.db import get_db
from app.main import app

H = {"X-API-Key": "test-key"}


class FakeResult:
    def __init__(self, stmt):
        cols = [c.key or f"c{i}" for i, c in enumerate(stmt.selected_columns)]
        self.row = namedtuple("Row", cols, rename=True)(*([0] * len(cols)))

    def all(self):
        return []

    def one(self):
        return self.row


class FakeSession:
    def __init__(self):
        self.sql = []

    def _compile(self, stmt):
        self.sql.append(str(stmt.compile(dialect=postgresql.dialect())))

    def execute(self, stmt):
        self._compile(stmt)
        return FakeResult(stmt)

    def scalar(self, stmt):
        self._compile(stmt)
        return 0

    def scalars(self, stmt):
        self._compile(stmt)
        return FakeResult(stmt)


@pytest.fixture
def client():
    fake = FakeSession()
    app.dependency_overrides[get_db] = lambda: fake
    with TestClient(app) as c:
        c.fake = fake
        yield c
    app.dependency_overrides.clear()


def test_requires_api_key(client):
    assert client.get("/returns").status_code == 401


@pytest.mark.parametrize(
    "path",
    [
        "/reports/summary?date_from=2026-10-01&date_to=2026-10-13&platform=tiktok",
        "/reports/by-reason",
        "/reports/by-product?limit=5",
        "/reports/by-store",
        "/reports/rejections",
        "/reports/timeseries?granularity=week",
        "/reports/by-condition",
        "/reports/costs",
        "/reports/costs?group_by=sku",
        "/reports/costs?group_by=month",
        "/reports/costs?group_by=store",
    ],
)
def test_reports_build_valid_sql(client, path):
    r = client.get(path, headers=H)
    assert r.status_code == 200, r.text
    assert client.fake.sql


def test_list_returns_filters(client):
    r = client.get(
        "/returns?status=pending_review&needs_action=true&overdue=true&q=CAM-AZ&sort=action_deadline"
        "&condition=defect&erp_invoice_status=none&has_erp_error=true",
        headers=H,
    )
    assert r.status_code == 200, r.text
    assert r.json()["total"] == 0


def test_jobs_requires_api_key(client):
    assert client.post("/jobs/run").status_code == 401


def test_webhook_rejects_bad_signature(client):
    r = client.post("/webhooks/tiktok", content=b'{"type":12}', headers={"Authorization": "bad"})
    assert r.status_code == 401
