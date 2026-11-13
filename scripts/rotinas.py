"""Roda as rotinas periódicas sem a API no ar (mesmo que POST /jobs/run).

Uso provisório enquanto a API não está publicada: o Agendador de Tarefas do Windows chama este script
a cada 15 minutos (ver deploy/agendar-rotinas-windows.ps1). O resultado vai para logs/rotinas.log.

    .venv\\Scripts\\python scripts\\rotinas.py
"""

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # o .env é lido da pasta do projeto

LOG_DIR = ROOT / "logs"
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    handlers=[RotatingFileHandler(LOG_DIR / "rotinas.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")],
)
# O httpx loga URLs completas (a Shopee exige o access_token na query string)
logging.getLogger("httpx").setLevel(logging.WARNING)

from app.db import new_session  # noqa: E402
from app.routers.jobs import run_jobs  # noqa: E402

log = logging.getLogger("rotinas")


def main() -> None:
    log.info("Início das rotinas")
    with new_session() as db:
        results = run_jobs(db)
    for name, r in results.items():
        if r["ok"]:
            log.info("%s ok em %ss: %s", name, r["seconds"], json.dumps(r["result"], ensure_ascii=False, default=str)[:500])
        else:
            log.error("%s FALHOU em %ss: %s", name, r["seconds"], r["error"])
    log.info("Fim das rotinas")


if __name__ == "__main__":
    main()
