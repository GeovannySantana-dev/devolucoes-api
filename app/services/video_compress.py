"""Recomprime os vídeos das conferências no servidor (rotina a cada 15 min).

O vídeo do celular chega com ~70 MB (1080p, bitrate alto). Recomprimido em H.264 com o lado menor em até
VIDEO_MAX_SIDE px fica ~10x menor e continua mostrando a peça e a etiqueta. O original só é trocado depois
que o novo arquivo saiu inteiro e menor; se não ficar menor, o original fica como está.
Só para MEDIA_BACKEND=local (a pasta MEDIA_DIR do PC servidor).
"""

import logging
import os
import re
import shutil
import subprocess
from datetime import UTC, datetime
from functools import lru_cache
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ReturnMedia
from .media import LocalStorage, get_storage

log = logging.getLogger(__name__)
PER_RUN = 5  # vídeos por rodada: cada um leva de segundos a 1-2 min no PC servidor
MIN_OUTPUT = 50 * 1024  # saída menor que isso é falha do ffmpeg, não compressão
MAX_DURATION_DIFF = 1.0  # segundos de diferença aceitos entre o original e o comprimido


@lru_cache
def ffmpeg_exe() -> str:
    """O ffmpeg do sistema (FFMPEG_PATH) ou, se não houver, o que vem no pacote imageio-ffmpeg."""
    found = shutil.which(get_settings().ffmpeg_path)
    if found:
        return found
    import imageio_ffmpeg

    return imageio_ffmpeg.get_ffmpeg_exe()


def duration(path: Path) -> float | None:
    """Duração em segundos, lida do cabeçalho pelo ffmpeg ("Duration: 00:01:02.50")."""
    proc = subprocess.run([ffmpeg_exe(), "-hide_banner", "-i", str(path)], capture_output=True, text=True, timeout=60)
    m = re.search(r"Duration: (\d+):(\d+):(\d+(?:\.\d+)?)", proc.stderr or "")
    return int(m[1]) * 3600 + int(m[2]) * 60 + float(m[3]) if m else None


def compress_file(src: Path, dst: Path, max_side: int, crf: int) -> None:
    # Lado menor até max_side (vídeo em pé ou deitado), sem aumentar vídeo pequeno; aspas protegem as vírgulas
    scale = (f"scale='if(gt(iw,ih),-2,min({max_side},iw))':'if(gt(iw,ih),min({max_side},ih),-2)'")
    cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-i", str(src), "-vf", scale,
           "-c:v", "libx264", "-preset", "veryfast", "-crf", str(crf), "-pix_fmt", "yuv420p",
           "-c:a", "aac", "-b:a", "64k", "-movflags", "+faststart", str(dst)]
    proc = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
    if proc.returncode != 0 or not dst.exists() or dst.stat().st_size < MIN_OUTPUT:
        dst.unlink(missing_ok=True)
        raise ValueError(f"ffmpeg falhou: {(proc.stderr or '').strip()[:300]}")
    # O original será apagado: a versão menor precisa ter o vídeo inteiro
    before, after = duration(src), duration(dst)
    if before is None or after is None or abs(before - after) > MAX_DURATION_DIFF:
        dst.unlink(missing_ok=True)
        raise ValueError(f"duração não confere: original {before} s, comprimido {after} s")


def compress_pending(db: Session, limit: int = PER_RUN) -> dict:
    s = get_settings()
    storage = get_storage()
    if not s.video_compress or not isinstance(storage, LocalStorage):
        return {"skipped": "compressão desligada ou mídia fora deste PC"}
    pending = db.scalars(
        select(ReturnMedia).where(ReturnMedia.kind == "video", ReturnMedia.compressed_at.is_(None))
        .order_by(ReturnMedia.created_at).limit(limit)
    ).all()
    out = {"comprimidos": 0, "mb_antes": 0.0, "mb_depois": 0.0, "falhas": 0}
    for m in pending:
        src = storage.path(m.storage_path)
        if not src.exists():
            m.compressed_at = datetime.now(UTC)  # sem arquivo: não tenta de novo a cada rodada
            db.commit()
            continue
        new_path = str(Path(m.storage_path).with_suffix(".mp4")).replace("\\", "/")
        tmp = src.with_name(src.stem + ".comprimindo.mp4")
        before = src.stat().st_size
        try:
            compress_file(src, tmp, s.video_max_side, s.video_crf)
        except (ValueError, subprocess.TimeoutExpired, OSError) as exc:
            log.warning("Vídeo %s não comprimido: %s", m.storage_path, exc)
            tmp.unlink(missing_ok=True)
            out["falhas"] += 1
            continue
        after = tmp.stat().st_size
        if after >= before:  # já estava leve: fica o original
            tmp.unlink(missing_ok=True)
            m.compressed_at = datetime.now(UTC)
            db.commit()
            continue
        try:
            os.replace(tmp, storage.path(new_path))  # aberto no player agora? tenta de novo na próxima rodada
        except OSError as exc:
            log.warning("Vídeo %s em uso, troca na próxima rodada: %s", m.storage_path, exc)
            tmp.unlink(missing_ok=True)
            continue
        if new_path != m.storage_path:
            src.unlink(missing_ok=True)
        m.storage_path, m.content_type = new_path, "video/mp4"
        m.original_size_bytes, m.size_bytes = before, after
        m.compressed_at = datetime.now(UTC)
        db.commit()
        out["comprimidos"] += 1
        out["mb_antes"] += round(before / 1048576, 1)
        out["mb_depois"] += round(after / 1048576, 1)
    return out
