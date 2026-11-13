# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Cópia das fotos e vídeos das conferências no Supabase Storage (bucket privado MEDIA_BACKUP_BUCKET).

Roda a cada 15 min nas rotinas: manda o que ainda não foi copiado, no mesmo caminho da pasta local
("plataforma/número do pedido/arquivo"). Se o PC servidor estragar, as provas continuam lá.
- Plano grátis do Supabase (1 GB no total): só fotos, até MEDIA_BACKUP_MAX_GB; passando disso, para e avisa.
- Plano pago: MEDIA_BACKUP_VIDEOS=true copia também os vídeos, depois de comprimidos.
"""

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from urllib.parse import quote

import httpx
from sqlalchemy import func, or_, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import ReturnMedia
from .media import get_storage

log = logging.getLogger(__name__)
PER_RUN = 300


def is_configured(s: Settings) -> bool:
    return bool(s.supabase_url and s.supabase_service_key)


def _eligible(s: Settings):
    """Mídias que entram na cópia: fotos sempre; vídeos só com o plano pago e depois de comprimidos."""
    if s.media_backup_videos:
        return or_(ReturnMedia.kind != "video", ReturnMedia.compressed_at.is_not(None))
    return ReturnMedia.kind != "video"


def upload(client: httpx.Client, s: Settings, path: str, content: bytes, content_type: str | None) -> None:
    url = f"{s.supabase_url.rstrip('/')}/storage/v1/object/{s.media_backup_bucket}/{quote(path)}"
    headers = {"Authorization": f"Bearer {s.supabase_service_key}", "apikey": s.supabase_service_key,
               "x-upsert": "true", "Content-Type": content_type or "application/octet-stream"}
    resp = client.post(url, content=content, headers=headers)
    if resp.status_code >= 400:
        raise RuntimeError(f"Supabase Storage {resp.status_code}: {resp.text[:200]}")


def run_backup(db: Session, settings: Settings | None = None, client: httpx.Client | None = None) -> dict:
    s = settings or get_settings()
    if not is_configured(s):
        return {"skipped": "falta SUPABASE_URL / SUPABASE_SERVICE_KEY no .env"}
    limit = int(s.media_backup_max_gb * 1024**3)
    used = db.scalar(select(func.coalesce(func.sum(ReturnMedia.size_bytes), 0)).where(ReturnMedia.backup_at.is_not(None)))
    pending = db.scalars(
        select(ReturnMedia).where(ReturnMedia.backup_at.is_(None), _eligible(s))
        .order_by(ReturnMedia.created_at).limit(PER_RUN)
    ).all()
    storage = get_storage()
    out = {"copiados": 0, "falhas": 0, "cheio": False}
    own = client is None
    client = client or httpx.Client(timeout=120)
    try:
        for m in pending:
            size = m.size_bytes or 0
            if used + size > limit:
                out["cheio"] = True  # espaço do plano acabou: health avisa por e-mail
                break
            content = storage.read_bytes(m.storage_path)
            if content is None:
                continue  # arquivo sumiu do PC: nada a copiar
            try:
                upload(client, s, m.storage_path, content, m.content_type)
            except (httpx.HTTPError, RuntimeError) as exc:
                log.warning("Cópia de %s falhou: %s", m.storage_path, exc)
                out["falhas"] += 1
                if out["falhas"] >= 3:
                    break  # Supabase fora ou chave errada: tenta de novo na próxima rodada
                continue
            m.backup_at = datetime.now(UTC)
            db.commit()
            used += size
            out["copiados"] += 1
    finally:
        if own:
            client.close()
    return out


@dataclass
class BackupStatus:
    configured: bool
    videos: bool
    total: int
    copied: int
    waiting: int  # vídeos que esperam o plano pago
    used_bytes: int
    limit_bytes: int
    last: datetime | None

    @property
    def level(self) -> str:
        if not self.configured:
            return "off"
        if self.used_bytes >= self.limit_bytes * 0.95:
            return "bad"
        return "ok" if self.copied >= self.total - self.waiting else "warn"


def status(db: Session, settings: Settings | None = None) -> BackupStatus:
    s = settings or get_settings()
    row = db.execute(select(
        func.count(),
        func.count().filter(ReturnMedia.backup_at.is_not(None)),
        func.count().filter(ReturnMedia.backup_at.is_(None), ~_eligible(s)),
        func.coalesce(func.sum(ReturnMedia.size_bytes).filter(ReturnMedia.backup_at.is_not(None)), 0),
        func.max(ReturnMedia.backup_at),
    )).one()
    return BackupStatus(is_configured(s), s.media_backup_videos, row[0], row[1], row[2], int(row[3]),
                        int(s.media_backup_max_gb * 1024**3), row[4])
