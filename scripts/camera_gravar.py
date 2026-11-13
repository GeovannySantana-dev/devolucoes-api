# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Gravação contínua da câmera da bancada (roda no computador servidor).

Lê o vídeo da câmera IP (RTSP, CAMERA_RTSP_URL no .env) e grava sem converter, em arquivos de 1 minuto:
  CAMERA_DIR/AAAAMMDD/AAAAMMDD-HHMMSS.mp4   (horário local do servidor, alinhado ao relógio)
Se a câmera cair, tenta de novo a cada 15 s. Uma vez por hora apaga o que passou de CAMERA_RETENTION_DAYS.
O recorte de cada conferência é feito pela rotina de 15 min (app/services/camera.py).

    .venv\\Scripts\\python scripts\\camera_gravar.py        (a tarefa "Devolucoes - camera da bancada" roda isto)
"""

import logging
import os
import subprocess
import sys
import time
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)

(ROOT / "logs").mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[RotatingFileHandler(ROOT / "logs" / "camera.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")],
)
log = logging.getLogger("camera")

from app.config import get_settings  # noqa: E402
from app.services.camera import cleanup_recordings, recordings_dir  # noqa: E402
from app.services.video_compress import ffmpeg_exe  # noqa: E402


def ffmpeg_cmd() -> list[str]:
    s = get_settings()
    pattern = recordings_dir() / "%Y%m%d" / "%Y%m%d-%H%M%S.mp4"
    return [
        ffmpeg_exe(), "-hide_banner", "-loglevel", "warning",
        "-rtsp_transport", "tcp", "-i", s.camera_rtsp_url,
        "-an", "-c:v", "copy",  # sem áudio, sem converter
        "-f", "segment", "-segment_time", str(s.camera_segment_seconds), "-segment_atclocktime", "1",
        "-reset_timestamps", "1", "-strftime", "1", "-strftime_mkdir", "1", str(pattern),
    ]


def main() -> None:
    if not get_settings().camera_rtsp_url:
        log.error("CAMERA_RTSP_URL vazio no .env: câmera desligada")
        return
    recordings_dir().mkdir(parents=True, exist_ok=True)
    last_cleanup = 0.0
    while True:
        log.info("Iniciando gravação em %s", recordings_dir())  # nunca registra o endereço (tem senha)
        proc = subprocess.Popen(ffmpeg_cmd(), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True)
        while proc.poll() is None:
            if time.time() - last_cleanup > 3600:
                removed = cleanup_recordings()
                if removed:
                    log.info("Apagadas %s pastas de gravação antigas", removed)
                last_cleanup = time.time()
            time.sleep(30)
        err = (proc.stderr.read() if proc.stderr else "")[-500:]
        log.warning("Gravação parou (código %s). %s. Tentando de novo em 15 s", proc.returncode,
                    err.replace(get_settings().camera_rtsp_url, "<camera>"))
        time.sleep(15)


if __name__ == "__main__":
    main()
