# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Cifra os tokens das lojas que ainda estão em texto puro no banco (usa TOKEN_ENCRYPTION_KEY do .env).

    .venv\\Scripts\\python scripts\\criptografar_tokens.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select, text  # noqa: E402
from sqlalchemy.orm.attributes import flag_modified  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.crypto import PREFIX  # noqa: E402
from app.db import new_session  # noqa: E402
from app.models import Store  # noqa: E402


def main() -> None:
    if not get_settings().token_encryption_key:
        sys.exit("Defina TOKEN_ENCRYPTION_KEY no .env primeiro.")
    with new_session() as db:
        for store in db.scalars(select(Store)).all():
            flag_modified(store, "access_token")  # regrava: o tipo da coluna cifra ao salvar
            flag_modified(store, "refresh_token")
        db.commit()
        rows = db.execute(text("select name, left(access_token, 7), left(refresh_token, 7) from stores")).all()
    for name, a, r in rows:
        ok = all(v is None or v == PREFIX for v in (a, r))
        print(f"{'ok ' if ok else 'ERRO'} {name}")


if __name__ == "__main__":
    main()
