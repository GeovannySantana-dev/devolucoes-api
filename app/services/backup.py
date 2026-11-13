# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Backup diário do banco (o plano gratuito do Supabase não guarda backups restauráveis).

Uma vez por dia, dentro das rotinas: exporta as tabelas do sistema para um JSON compactado.
- MEDIA_BACKEND=local: grava em backups/auto/ neste computador.
- MEDIA_BACKEND=gcs: grava no bucket, em backups/ (o disco do Cloud Run é temporário).
Guarda os últimos BACKUP_KEEP_DAYS dias. Tokens das lojas não entram (basta autorizar de novo).
As fotos/vídeos não entram aqui: ficam na pasta media/ ou no bucket.
"""

import gzip
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from sqlalchemy import text

from ..config import get_settings
from ..db import get_engine

log = logging.getLogger(__name__)

TABLES = [
    "stores", "returns", "return_items", "return_events", "return_media", "rejection_reasons",
    "return_reason_rules", "users", "alerts_sent", "api_keys", "webhook_events",
]
SECRET_COLUMNS = {"access_token", "refresh_token"}
LOCAL_DIR = Path("backups") / "auto"
PREFIX = "devolucoes-"


def _dump() -> bytes:
    data: dict = {"created_at": datetime.now(UTC).isoformat(), "tables": {}}
    with get_engine().connect() as conn:
        for table in TABLES:
            cols = [
                r[0]
                for r in conn.execute(
                    text("select column_name from information_schema.columns "
                         "where table_schema = 'public' and table_name = :t order by ordinal_position"),
                    {"t": table},
                )
                if r[0] not in SECRET_COLUMNS
            ]
            if not cols:
                continue
            sql = f'select coalesce(json_agg(t), \'[]\'::json) from (select {", ".join(chr(34) + c + chr(34) for c in cols)} from public."{table}") t'
            data["tables"][table] = conn.execute(text(sql)).scalar()
    return gzip.compress(json.dumps(data, ensure_ascii=False, default=str).encode())


def run_daily_backup(now: datetime | None = None) -> dict:
    s = get_settings()
    now = now or datetime.now(UTC)
    day = now.astimezone(ZoneInfo(s.timezone)).strftime("%Y-%m-%d")
    name = f"{PREFIX}{day}.json.gz"

    if s.media_backend == "gcs":
        from .media import get_storage

        bucket = get_storage().bucket
        blob = bucket.blob(f"backups/{name}")
        if blob.exists():
            return {"skipped": "backup de hoje já existe", "file": f"gs://{s.gcs_bucket}/backups/{name}"}
        content = _dump()
        blob.upload_from_string(content, content_type="application/gzip")
        old = sorted(b.name for b in bucket.list_blobs(prefix=f"backups/{PREFIX}"))
        for stale in old[: max(0, len(old) - s.backup_keep_days)]:
            bucket.blob(stale).delete()
        return {"file": f"gs://{s.gcs_bucket}/backups/{name}", "kb": len(content) // 1024}

    LOCAL_DIR.mkdir(parents=True, exist_ok=True)
    target = LOCAL_DIR / name
    if target.exists():
        return {"skipped": "backup de hoje já existe", "file": str(target)}
    content = _dump()
    tmp = target.with_suffix(".tmp")
    tmp.write_bytes(content)
    tmp.replace(target)  # só aparece completo
    old = sorted(LOCAL_DIR.glob(f"{PREFIX}*.json.gz"))
    for stale in old[: max(0, len(old) - s.backup_keep_days)]:
        stale.unlink()
    return {"file": str(target), "kb": len(content) // 1024}
