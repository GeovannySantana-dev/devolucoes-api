"""Câmera da bancada: recorta, da gravação contínua, o trecho de cada conferência e anexa na devolução.

A gravação em si roda em scripts/camera_gravar.py (ffmpeg, arquivos de 1 minuto em
CAMERA_DIR/AAAAMMDD/AAAAMMDD-HHMMSS.mp4, no horário local do servidor). Aqui:

- início: quando o operador abre a devolução para conferir (`inspection_started_at`);
- fim: quando confirma a conferência (`received_at`);
- recorte: [início - folga, fim + folga], copiado sem converter (rápido e sem perder qualidade), salvo como
  vídeo da devolução com origem "camera" ("Vídeo da bancada").
"""

import logging
import shutil
import subprocess
import tempfile
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..enums import EventType
from ..models import Return, ReturnEvent, ReturnMedia
from .media import get_storage, media_path
from .video_compress import ffmpeg_exe

log = logging.getLogger(__name__)
NAME_FORMAT = "%Y%m%d-%H%M%S"
LOOKBACK = timedelta(days=2)  # conferências mais antigas que isso não são mais recortadas


def enabled() -> bool:
    return bool(get_settings().camera_rtsp_url)


def recordings_dir() -> Path:
    return Path(get_settings().camera_dir).resolve()


@dataclass
class Segment:
    path: Path
    start: datetime  # com fuso (horário local do servidor)


def list_segments(root: Path, start: datetime, end: datetime, segment_seconds: int) -> list[Segment]:
    """Arquivos de gravação que cobrem [start, end], em ordem."""
    out = []
    day = (start - timedelta(seconds=segment_seconds)).astimezone().date()
    while day <= end.astimezone().date():
        folder = root / day.strftime("%Y%m%d")
        for f in sorted(folder.glob("*.mp4")) if folder.is_dir() else []:
            try:
                s = datetime.strptime(f.stem, NAME_FORMAT).astimezone()  # nome no horário local
            except ValueError:
                continue
            if s < end and s + timedelta(seconds=segment_seconds) > start:
                out.append(Segment(f, s))
        day += timedelta(days=1)
    return sorted(out, key=lambda s: s.start)


def clip_window(ret: Return) -> tuple[datetime, datetime]:
    pad = timedelta(seconds=get_settings().camera_clip_pad_seconds)
    return ret.inspection_started_at - pad, ret.received_at + pad


def make_clip(ret: Return, out_file: Path) -> None:
    """Gera o mp4 do trecho da conferência em `out_file`. Levanta ValueError se não houver gravação."""
    s = get_settings()
    start, end = clip_window(ret)
    if end - start > timedelta(minutes=s.camera_max_clip_minutes):
        start = end - timedelta(minutes=s.camera_max_clip_minutes)  # conferência aberta há muito tempo
    segments = list_segments(recordings_dir(), start, end, s.camera_segment_seconds)
    if not segments:
        raise ValueError("sem gravação da câmera nesse horário")
    with tempfile.TemporaryDirectory() as tmp:
        listing = Path(tmp) / "lista.txt"
        listing.write_text("".join(f"file '{seg.path.as_posix()}'\n" for seg in segments), encoding="utf-8")
        offset = max(0.0, (start - segments[0].start).total_seconds())
        duration = (end - start).total_seconds()
        cmd = [ffmpeg_exe(), "-hide_banner", "-loglevel", "error", "-y", "-f", "concat", "-safe", "0",
               "-i", str(listing), "-ss", f"{offset:.1f}", "-t", f"{duration:.1f}", "-c", "copy",
               "-movflags", "+faststart", str(out_file)]
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=300)
        if proc.returncode != 0 or not out_file.exists() or out_file.stat().st_size == 0:
            raise ValueError(f"ffmpeg falhou: {(proc.stderr or '').strip()[:300]}")


def attach_clip(db: Session, ret: Return) -> ReturnMedia:
    with tempfile.TemporaryDirectory() as tmp:
        out = Path(tmp) / "bancada.mp4"
        make_clip(ret, out)
        media_id = uuid.uuid4()
        path = media_path(ret, media_id, "video_bancada.mp4")
        with out.open("rb") as fh:  # sem o limite de upload do celular: o vídeo vem do próprio servidor
            size = get_storage().save(path, fh, 2 * 1024**3, "video/mp4")
    media = ReturnMedia(id=media_id, return_id=ret.id, kind="video", source="camera", storage_path=path,
                        content_type="video/mp4", size_bytes=size, original_name="Vídeo da bancada.mp4",
                        uploaded_by="câmera da bancada")
    db.add(media)
    ret.events.append(ReturnEvent(event_type=EventType.MEDIA_ADDED, source="camera", actor="câmera da bancada",
                                  message="Vídeo da bancada anexado (gravação da conferência)",
                                  payload={"media_id": str(media_id)}))
    return media


def process_pending_clips(db: Session, limit: int = 30) -> dict:
    """Rotina: recorta o vídeo das conferências já confirmadas."""
    if not enabled():
        return {"camera": "desligada"}
    s = get_settings()
    now = datetime.now(UTC)
    ready = now - timedelta(seconds=s.camera_clip_pad_seconds + s.camera_segment_seconds + 30)  # trecho já gravado
    rows = db.scalars(
        select(Return).where(
            Return.inspection_started_at.is_not(None),
            Return.received_at.is_not(None),
            Return.camera_clip_at.is_(None),
            Return.received_at < ready,
            Return.received_at > now - LOOKBACK,
        ).limit(limit)
    ).all()
    done = failed = 0
    for ret in rows:
        try:
            attach_clip(db, ret)
            ret.camera_clip_error = None
            done += 1
        except Exception as exc:  # sem gravação, ffmpeg etc.: registra e não tenta de novo
            log.warning("Vídeo da bancada da devolução %s: %s", ret.id, exc)
            ret.camera_clip_error = str(exc)[:300]
            failed += 1
        ret.camera_clip_at = datetime.now(UTC)
        db.commit()
    return {"recortados": done, "sem_video": failed}


def cleanup_recordings(now: datetime | None = None) -> int:
    """Apaga as pastas de gravação mais antigas que CAMERA_RETENTION_DAYS. Devolve quantas apagou."""
    root = recordings_dir()
    if not root.is_dir():
        return 0
    limit = ((now or datetime.now()).astimezone() - timedelta(days=get_settings().camera_retention_days)).date()
    removed = 0
    for folder in root.iterdir():
        try:
            day = datetime.strptime(folder.name, "%Y%m%d").date()
        except ValueError:
            continue
        if folder.is_dir() and day < limit:
            shutil.rmtree(folder, ignore_errors=True)
            removed += 1
    return removed
