# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Autoriza a loja Shopee sem precisar da API no ar.

1. Rode o script: ele mostra o link de autorização.
2. Abra o link logado na conta principal da loja no Seller Center e autorize.
3. A Shopee redireciona para outra página (pode dar erro de carregamento, não tem problema).
   Copie a URL inteira da barra de endereço (tem ?code=...&shop_id=...) e cole aqui.
   O código vale por 10 minutos e só pode ser usado uma vez.

    .venv\\Scripts\\python scripts\\shopee_autorizar.py [url_de_redirecionamento]
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import new_session  # noqa: E402
from app.platforms.base import PlatformError  # noqa: E402
from app.platforms.registry import get_shopee_client  # noqa: E402
from app.services.shopee_auth import authorize_with_code  # noqa: E402


def main() -> None:
    redirect = sys.argv[1] if len(sys.argv) > 1 else get_settings().public_base_url
    try:
        url = get_shopee_client().authorize_url(redirect)
    except PlatformError as exc:
        sys.exit(str(exc))
    print(f"\nAbra este link logado na loja (vale por 5 minutos):\n\n{url}\n")

    qs = parse_qs(urlparse(input("Cole a URL da barra de endereço após autorizar: ").strip()).query)
    if "code" not in qs or "shop_id" not in qs:
        sys.exit("A URL colada não tem os parâmetros 'code' e 'shop_id'.")

    with new_session() as db:
        try:
            store = authorize_with_code(db, qs["code"][0], qs["shop_id"][0])
        except PlatformError as exc:
            sys.exit(f"\nFalha na autorização: {exc}")
        print(f"\nLoja autorizada: {store.name} (shop_id {store.external_shop_id})")
        print(f"Token válido até {store.access_token_expires_at:%d/%m/%Y %H:%M} (renovado automaticamente)")


if __name__ == "__main__":
    main()
