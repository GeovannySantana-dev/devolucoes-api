"""Fotos e vídeos das devoluções.

Dois armazenamentos com a mesma interface, escolhidos por MEDIA_BACKEND:
- `local`: pasta MEDIA_DIR neste computador (provisório; no Cloud Run o disco é temporário).
- `gcs`: Google Cloud Storage, bucket GCS_BUCKET (no Cloud Run usa a conta de serviço; localmente,
  `gcloud auth application-default login`). Para migrar o que já existe: scripts/migrar_midias_gcs.py.
O `storage_path` gravado no banco é o mesmo nos dois: "<plataforma>/<número do pedido>/<id curto>_foto.jpg",
para achar as fotos de um pedido direto na pasta do backup. Fotos antigas ficavam em "<id da devolução>/...";
scripts/renomear_pastas_midias.py move as que já existem.
"""

import mimetypes
import re
import tempfile
import uuid
from collections.abc import Iterator
from functools import lru_cache
from pathlib import Path
from typing import BinaryIO, Protocol

from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import EventType
from ..models import Return, ReturnEvent, ReturnMedia

ALLOWED_PREFIXES = ("image/", "video/")
CHUNK = 1024 * 1024


class MediaError(Exception):
    pass


def _too_big(max_bytes: int) -> MediaError:
    return MediaError(f"Arquivo maior que o limite de {max_bytes // (1024 * 1024)} MB")


def _check_path(storage_path: str) -> str:
    parts = Path(storage_path).parts
    if not parts or storage_path.startswith(("/", "\\")) or ".." in parts:
        raise MediaError("Caminho de arquivo inválido")
    return "/".join(parts)


class Storage(Protocol):
    def save(self, storage_path: str, stream: BinaryIO, max_bytes: int, content_type: str | None = None) -> int: ...
    def read_bytes(self, storage_path: str) -> bytes | None: ...
    def iter_chunks(self, storage_path: str) -> Iterator[bytes] | None: ...
    def delete(self, storage_path: str) -> None: ...


class LocalStorage:
    def __init__(self, root: str | Path):
        self.root = Path(root).resolve()

    def path(self, storage_path: str) -> Path:
        p = (self.root / storage_path).resolve()
        if self.root not in p.parents:  # nunca sai da pasta de mídia
            raise MediaError("Caminho de arquivo inválido")
        return p

    def save(self, storage_path: str, stream: BinaryIO, max_bytes: int, content_type: str | None = None) -> int:
        target = self.path(storage_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        size = 0
        try:
            with open(target, "wb") as out:
                while chunk := stream.read(CHUNK):
                    size += len(chunk)
                    if size > max_bytes:
                        raise _too_big(max_bytes)
                    out.write(chunk)
        except BaseException:
            target.unlink(missing_ok=True)
            raise
        return size

    def read_bytes(self, storage_path: str) -> bytes | None:
        p = self.path(storage_path)
        return p.read_bytes() if p.exists() else None

    def iter_chunks(self, storage_path: str) -> Iterator[bytes] | None:
        p = self.path(storage_path)
        if not p.exists():
            return None

        def chunks():
            with open(p, "rb") as f:
                while chunk := f.read(CHUNK):
                    yield chunk

        return chunks()

    def delete(self, storage_path: str) -> None:
        self.path(storage_path).unlink(missing_ok=True)


class GCSStorage:
    """Google Cloud Storage. Os objetos ficam em gs://<bucket>/media/<storage_path>, privados."""

    PREFIX = "media/"

    def __init__(self, bucket_name: str, client=None):
        if not bucket_name:
            raise MediaError("MEDIA_BACKEND=gcs exige GCS_BUCKET")
        if client is None:
            from google.cloud import storage  # import tardio: só quem usa GCS precisa da credencial

            client = storage.Client()
        self.bucket = client.bucket(bucket_name)

    def _blob(self, storage_path: str):
        return self.bucket.blob(self.PREFIX + _check_path(storage_path))

    def save(self, storage_path: str, stream: BinaryIO, max_bytes: int, content_type: str | None = None) -> int:
        size = 0
        # Copia para um arquivo temporário conferindo o limite, depois envia de uma vez
        with tempfile.SpooledTemporaryFile(max_size=8 * CHUNK) as tmp:
            while chunk := stream.read(CHUNK):
                size += len(chunk)
                if size > max_bytes:
                    raise _too_big(max_bytes)
                tmp.write(chunk)
            tmp.seek(0)
            self._blob(storage_path).upload_from_file(tmp, size=size, content_type=content_type)
        return size

    def read_bytes(self, storage_path: str) -> bytes | None:
        from google.api_core.exceptions import NotFound

        try:
            return self._blob(storage_path).download_as_bytes()
        except NotFound:
            return None

    def iter_chunks(self, storage_path: str) -> Iterator[bytes] | None:
        blob = self._blob(storage_path)
        if not blob.exists():
            return None

        def chunks():
            with blob.open("rb", chunk_size=CHUNK) as f:
                while chunk := f.read(CHUNK):
                    yield chunk

        return chunks()

    def delete(self, storage_path: str) -> None:
        from google.api_core.exceptions import NotFound

        try:
            self._blob(storage_path).delete()
        except NotFound:
            pass


@lru_cache
def _gcs(bucket: str) -> GCSStorage:
    return GCSStorage(bucket)


def get_storage() -> Storage:
    s = get_settings()
    if s.media_backend == "gcs":
        return _gcs(s.gcs_bucket)
    return LocalStorage(s.media_dir)


def _safe_name(name: str | None) -> str:
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", Path(name or "arquivo").name)[-80:]
    return stem or "arquivo"


def _folder_part(text: str) -> str:
    return re.sub(r"[^A-Za-z0-9._-]+", "_", text).strip("._")[:80]


def return_folder(ret: Return) -> str:
    """Pasta das mídias da devolução: plataforma e número do pedido (ou da devolução, se não houver pedido)."""
    number = _folder_part(ret.external_order_id or "") or _folder_part(ret.external_return_id or "") or str(ret.id)
    return f"{_folder_part(ret.platform) or 'outros'}/{number}"


def media_path(ret: Return, media_id: uuid.UUID, filename: str) -> str:
    return f"{return_folder(ret)}/{media_id.hex[:8]}_{_safe_name(filename)}"


def save_media(
    db: Session,
    ret: Return,
    stream: BinaryIO,
    filename: str | None,
    content_type: str | None,
    uploaded_by: str | None,
    source: str = "inspection",
) -> ReturnMedia:
    content_type = content_type or mimetypes.guess_type(filename or "")[0] or ""
    if not content_type.startswith(ALLOWED_PREFIXES) or "svg" in content_type.lower():
        raise MediaError("Envie apenas fotos ou vídeos")
    kind = "video" if content_type.startswith("video/") else "photo"

    media_id = uuid.uuid4()
    storage_path = media_path(ret, media_id, filename or "arquivo")
    size = get_storage().save(storage_path, stream, get_settings().media_max_mb * 1024 * 1024, content_type)

    media = ReturnMedia(
        id=media_id,
        return_id=ret.id,
        kind=kind,
        source=source,
        storage_path=storage_path,
        content_type=content_type,
        size_bytes=size,
        original_name=filename,
        uploaded_by=uploaded_by,
    )
    db.add(media)
    ret.events.append(
        ReturnEvent(
            event_type=EventType.MEDIA_ADDED,
            source="api",
            actor=uploaded_by,
            message=f"{'Vídeo' if kind == 'video' else 'Foto'} adicionada",
            payload={"media_id": str(media_id)},
        )
    )
    db.commit()
    return media


def delete_media(db: Session, ret: Return, media: ReturnMedia, actor: str | None) -> None:
    get_storage().delete(media.storage_path)
    ret.events.append(
        ReturnEvent(
            event_type=EventType.MEDIA_REMOVED,
            source="api",
            actor=actor,
            message=f"{'Vídeo' if media.kind == 'video' else 'Foto'} removida",
            payload={"media_id": str(media.id), "original_name": media.original_name},
        )
    )
    db.delete(media)
    db.commit()


EVIDENCE_TYPES = {"image/jpeg", "image/jpg", "image/png"}
EVIDENCE_MAX_BYTES = 10 * 1024 * 1024


def evidence_photos(ret: Return, limit: int = 3) -> list[tuple[str, bytes, str]]:
    """Fotos da conferência aceitas como evidência pelos marketplaces (JPG/PNG até 10 MB), as mais recentes."""
    storage = get_storage()
    out = []
    for m in sorted(ret.media, key=lambda m: m.created_at or 0, reverse=True):
        if m.kind != "photo" or (m.content_type or "").lower() not in EVIDENCE_TYPES:
            continue
        if m.size_bytes and m.size_bytes > EVIDENCE_MAX_BYTES:
            continue
        content = storage.read_bytes(m.storage_path)
        if content is None:
            continue
        out.append((m.original_name or Path(m.storage_path).name, content, m.content_type))
        if len(out) >= limit:
            break
    return out


def buyer_media(ret: Return) -> list[dict[str, str]]:
    """Fotos/vídeos que o comprador enviou ao marketplace (vêm nos dados brutos da plataforma)."""
    raw = ret.raw or {}
    out: list[dict[str, str]] = []
    # Shopee
    for url in raw.get("image") or []:
        out.append({"kind": "photo", "url": url})
    for v in raw.get("buyer_videos") or []:
        if isinstance(v, dict) and v.get("video_url"):
            out.append({"kind": "video", "url": v["video_url"]})
    # Sistema legado
    legacy = raw.get("legacy") or {}
    for url in (legacy.get("fotos_cliente") or []) + (legacy.get("foto_urls") or []):
        if url and {"kind": "photo", "url": url} not in out:
            out.append({"kind": "photo", "url": url})
    for url in legacy.get("videos_cliente") or []:
        if url:
            out.append({"kind": "video", "url": url})
    # TikTok e Mercado Livre não trazem as evidências do comprador na devolução (ficam em outros endpoints)
    return out
