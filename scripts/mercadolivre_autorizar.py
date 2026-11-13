# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Autoriza a conta do Mercado Livre sem precisar da API no ar.

1. Rode sem argumentos para gerar o link de autorização:
       .venv\\Scripts\\python scripts\\mercadolivre_autorizar.py
2. Abra o link logado como TITULAR da conta e clique em Permitir.
3. Copie a URL inteira da barra de endereço (tem ?code=...&state=...) e rode:
       .venv\\Scripts\\python scripts\\mercadolivre_autorizar.py "URL_COPIADA"
   O código vale poucos minutos e só pode ser usado uma vez.
"""

import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db import new_session  # noqa: E402
from app.platforms.base import PlatformError  # noqa: E402
from app.security import check_oauth_state, make_oauth_state  # noqa: E402
from app.services import mercadolivre_auth  # noqa: E402


def main() -> None:
    if len(sys.argv) < 2:
        try:
            url = mercadolivre_auth.authorize_url(make_oauth_state())
        except PlatformError as exc:
            sys.exit(str(exc))
        print(f"\nAbra este link logado como titular da conta (vale por 15 minutos):\n\n{url}\n")
        print('Depois rode: .venv\\Scripts\\python scripts\\mercadolivre_autorizar.py "URL_DA_BARRA_DE_ENDERECO"')
        return

    qs = parse_qs(urlparse(sys.argv[1].strip()).query)
    if "error" in qs:
        sys.exit(f"O Mercado Livre recusou: {qs.get('error')} {qs.get('error_description')}")
    if "code" not in qs or "state" not in qs:
        sys.exit("A URL colada não tem os parâmetros 'code' e 'state'. Copie a URL inteira, entre aspas.")
    state = qs["state"][0]
    if not check_oauth_state(state):
        sys.exit("O link expirou (mais de 15 minutos). Gere outro rodando o script sem argumentos.")

    with new_session() as db:
        try:
            store = mercadolivre_auth.authorize_with_code(db, qs["code"][0], state)
        except PlatformError as exc:
            sys.exit(f"\nFalha na autorização: {exc}")
        print(f"\nConta autorizada: {store.name} (user_id {store.external_shop_id})")
        print(f"Token válido até {store.access_token_expires_at:%d/%m/%Y %H:%M} (renovado automaticamente)")


if __name__ == "__main__":
    main()
