# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Move as fotos/vídeos antigos da pasta "<id da devolução>/" para "<plataforma>/<número do pedido>/".

Só para MEDIA_BACKEND=local (a pasta MEDIA_DIR deste computador). Move o arquivo e atualiza o caminho no
banco, um por um; pode rodar de novo, o que já está no lugar novo é pulado. Sem --aplicar só mostra o plano.

    .venv\Scripts\python scripts\renomear_pastas_midias.py            (só mostra)
    .venv\Scripts\python scripts\renomear_pastas_midias.py --aplicar
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import new_session  # noqa: E402
from app.models import Return  # noqa: E402
from app.services.media import LocalStorage, media_path, return_folder  # noqa: E402


def main() -> None:
    apply = "--aplicar" in sys.argv
    if get_settings().media_backend != "local":
        sys.exit("Só serve para MEDIA_BACKEND=local")
    storage = LocalStorage(get_settings().media_dir)
    moved = skipped = missing = 0
    old_dirs = set()
    with new_session() as db:
        pairs = [(r, m) for r in db.scalars(select(Return).where(Return.media.any()).options(selectinload(Return.media)))
                 for m in r.media]
        for ret, m in pairs:
            if m.storage_path.startswith(return_folder(ret) + "/"):
                skipped += 1
                continue
            src = storage.path(m.storage_path)
            if not src.exists():
                missing += 1
                print(f"sem arquivo: {m.storage_path}")
                continue
            # o nome antigo é "<uuid da mídia>_<nome>": o novo mantém o nome, com o id curto na frente
            name = m.storage_path.rsplit("/", 1)[-1].split("_", 1)[-1]
            new = media_path(ret, uuid.UUID(str(m.id)), name)
            print(f"{m.storage_path}  ->  {new}")
            if apply:
                dst = storage.path(new)
                dst.parent.mkdir(parents=True, exist_ok=True)
                src.replace(dst)
                m.storage_path = new
                db.commit()  # um por um: se parar no meio, o banco bate com os arquivos já movidos
                old_dirs.add(src.parent)
            moved += 1
    for d in old_dirs:
        if d.exists() and not any(d.iterdir()):
            d.rmdir()
    verb = "Movidos" if apply else "A mover"
    print(f"\n{verb}: {moved} · já no lugar: {skipped} · sem arquivo: {missing}")
    if not apply and moved:
        print("Rode de novo com --aplicar para mover.")


if __name__ == "__main__":
    main()
