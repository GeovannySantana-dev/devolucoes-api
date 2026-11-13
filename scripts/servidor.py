# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Servidor das telas (neste PC, publicado na internet pelo Tailscale Funnel).

Escuta só neste computador (127.0.0.1): o acesso é pelo endereço https do Funnel, que repassa para cá.
Assim ninguém no Wi-Fi acessa direto por http://IP-DO-PC:8000, sem criptografia. Para voltar a liberar
na rede local (ex.: Funnel fora do ar), rode com a variável HOST=0.0.0.0.
O agendador interno fica desligado: as rotinas já rodam pela tarefa "Devolucoes - rotinas a cada 15 min".

    .venv\\Scripts\\python scripts\\servidor.py
"""

import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
os.chdir(ROOT)  # o .env e a pasta media/ ficam na pasta do projeto
os.environ["ENABLE_SCHEDULER"] = "false"  # evita rodar as rotinas em dobro

import uvicorn  # noqa: E402

if __name__ == "__main__":
    (ROOT / "logs").mkdir(exist_ok=True)
    if sys.stdout is None:  # pythonw (sem console): manda a saída para um arquivo
        sys.stdout = sys.stderr = open(ROOT / "logs" / "servidor.log", "a", encoding="utf-8", buffering=1)
    uvicorn.run("app.main:app", host=os.environ.get("HOST", "127.0.0.1"), port=int(os.environ.get("PORT", "8000")), log_level="info")
