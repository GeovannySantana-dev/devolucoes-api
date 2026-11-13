"""Roda a sincronização das lojas pela linha de comando (sem precisar da API no ar).

    .venv\\Scripts\\python scripts\\sincronizar.py            # todas as lojas ativas
    .venv\\Scripts\\python scripts\\sincronizar.py tiktok     # só uma plataforma
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.db import new_session  # noqa: E402
from app.models import Store  # noqa: E402
from app.services.sync import sync_store  # noqa: E402


def main() -> None:
    platform = sys.argv[1] if len(sys.argv) > 1 else None
    with new_session() as db:
        query = select(Store).where(Store.active, Store.access_token.is_not(None))
        if platform:
            query = query.where(Store.platform == platform)
        stores = db.scalars(query).all()
        if not stores:
            sys.exit("Nenhuma loja autorizada para sincronizar.")
        for store in stores:
            print(f"Sincronizando {store.name}...", flush=True)
            r = sync_store(db, store)
            print(f"  buscadas={r.fetched} novas={r.created} atualizadas={r.updated}")
            for e in r.errors:
                print(f"  ERRO: {e}")


if __name__ == "__main__":
    main()
