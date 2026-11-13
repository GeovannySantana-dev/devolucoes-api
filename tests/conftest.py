import os

os.environ.setdefault("DATABASE_URL", "postgresql+psycopg://test:test@localhost:5432/test")
os.environ.setdefault("API_KEY", "test-key")
os.environ.setdefault("TIKTOK_APP_KEY", "app-key")
os.environ.setdefault("TIKTOK_APP_SECRET", "app-secret")
os.environ.setdefault("ENABLE_SCHEDULER", "false")
# Os testes de NF não dependem das naturezas cadastradas no .env de quem roda os testes
os.environ["OLIST_RETURN_NATUREZAS"] = "-"  # "-" = nenhuma (vazio seria ignorado e o .env valeria)
os.environ["CAMERA_RTSP_URL"] = ""  # testes sem câmera, mesmo que o .env tenha

import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_cost_table():
    """Sem banco nos testes: custo por modelo vazio, toda peça usa o custo padrão (cada teste pode trocar)."""
    from app.services import product_costs

    product_costs.set_table({})
    yield
    product_costs.set_table({})
