# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Autoriza uma loja TikTok Shop colando o código de autorização manualmente.

Use enquanto a API não tem endereço público:
1. No Partner Center, copie o link de autorização do app e abra logado na loja.
2. Autorize. O navegador vai para a URL de redirecionamento com ?code=... na barra de endereço.
3. Rode este script e cole o valor de `code` (ou a URL inteira) quando pedir.
   O código expira em poucos minutos.

    .venv\\Scripts\\python scripts\\tiktok_autorizar.py
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import new_session  # noqa: E402
from app.platforms.base import PlatformError  # noqa: E402
from app.services.tiktok_auth import authorize_with_code  # noqa: E402


def extract_code(text: str) -> str:
    text = text.strip()
    if text.startswith("http"):
        qs = parse_qs(urlparse(text).query)
        if "code" not in qs:
            sys.exit("A URL colada não tem o parâmetro 'code'.")
        return qs["code"][0]
    return text


def main() -> None:
    code = extract_code(input("Cole o código (ou a URL inteira da barra de endereço): "))
    with new_session() as db:
        try:
            stores = authorize_with_code(db, code)
        except PlatformError as exc:
            sys.exit(f"\nFalha na autorização: {exc}")
    if not stores:
        print("\nAutorizado, mas nenhuma loja foi retornada pelo TikTok.")
    for s in stores:
        print(f"\nLoja autorizada: {s.name} (shop_id {s.external_shop_id}, região {s.region})")
        print(f"Token válido até {s.access_token_expires_at:%d/%m/%Y %H:%M}")


if __name__ == "__main__":
    main()
