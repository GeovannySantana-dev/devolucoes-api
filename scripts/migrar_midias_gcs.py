# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Copia as fotos/vídeos da pasta local (MEDIA_DIR) para o Google Cloud Storage (GCS_BUCKET).

Rodar uma vez no dia do deploy, antes de trocar MEDIA_BACKEND para gcs. O caminho gravado no banco não
muda, então nada precisa ser atualizado no banco. Pode rodar de novo: o que já está no bucket é pulado.
Precisa de credencial do Google: `gcloud auth application-default login`.

    .venv\\Scripts\\python scripts\\migrar_midias_gcs.py NOME-DO-BUCKET
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402

from app.config import get_settings  # noqa: E402
from app.db import new_session  # noqa: E402
from app.models import ReturnMedia  # noqa: E402
from app.services.media import GCSStorage, LocalStorage  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        sys.exit(__doc__)
    local = LocalStorage(get_settings().media_dir)
    gcs = GCSStorage(sys.argv[1])
    copied = skipped = missing = 0
    with new_session() as db:
        for m in db.scalars(select(ReturnMedia)).all():
            src = local.path(m.storage_path)
            if not src.exists():
                missing += 1
                continue
            if gcs._blob(m.storage_path).exists():
                skipped += 1
                continue
            with open(src, "rb") as f:
                gcs.save(m.storage_path, f, max_bytes=src.stat().st_size + 1, content_type=m.content_type)
            copied += 1
            print(f"ok {m.storage_path}")
    print(f"\nCopiados: {copied} · já estavam no bucket: {skipped} · sem arquivo local: {missing}")
    print("Agora defina MEDIA_BACKEND=gcs e GCS_BUCKET no Cloud Run.")


if __name__ == "__main__":
    main()
