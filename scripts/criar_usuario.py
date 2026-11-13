"""Cria um usuário das telas ou troca a senha de um existente. A senha é digitada sem aparecer na tela.

    .venv\\Scripts\\python scripts\\criar_usuario.py email@empresa.com "Nome da Pessoa" operador
    .venv\\Scripts\\python scripts\\criar_usuario.py email@empresa.com --senha    # troca a senha
    .venv\\Scripts\\python scripts\\criar_usuario.py email@empresa.com --desativar

Perfis: operador (confere o pacote, envia fotos/vídeos, aceita/recusa e gera a NF),
gestor (tudo, inclusive usuários e relatórios).
"""

import getpass
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import func, select  # noqa: E402

from app.db import new_session  # noqa: E402
from app.models import User  # noqa: E402
from app.services.users import ROLES, AuthError, create_user, set_password  # noqa: E402


def ask_password() -> str:
    pw = getpass.getpass("Senha (mín. 8 caracteres): ")
    if pw != getpass.getpass("Repita a senha: "):
        sys.exit("As senhas não conferem.")
    return pw


def main() -> None:
    args = sys.argv[1:]
    if not args:
        sys.exit(__doc__)
    email = args[0].strip().lower()
    with new_session() as db:
        user = db.scalar(select(User).where(func.lower(User.email) == email))
        try:
            if len(args) == 2 and args[1] in ("--senha", "--desativar", "--ativar"):
                if not user:
                    sys.exit(f"Usuário {email} não encontrado.")
                if args[1] == "--senha":
                    set_password(db, user, ask_password())
                    print(f"Senha de {user.name} alterada (sessões antigas foram encerradas).")
                else:
                    user.active = args[1] == "--ativar"
                    db.commit()
                    print(f"{user.name} {'ativado' if user.active else 'desativado'}.")
                return
            if len(args) != 3:
                sys.exit(__doc__)
            _, name, role = args
            if role not in ROLES:
                sys.exit(f"Perfil inválido. Use: {', '.join(ROLES)}")
            user = create_user(db, email, name, role, ask_password())
            print(f"Usuário criado: {user.name} <{user.email}> ({ROLES[user.role]})")
        except AuthError as exc:
            sys.exit(str(exc))


if __name__ == "__main__":
    main()
