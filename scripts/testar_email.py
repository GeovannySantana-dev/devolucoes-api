"""Envia um e-mail de teste com o resumo atual das devoluções, para conferir a configuração SMTP do .env.

    .venv\\Scripts\\python scripts\\testar_email.py
"""

import sys
from datetime import UTC, datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.db import new_session  # noqa: E402
from app.services import alerts  # noqa: E402


def main() -> None:
    s = get_settings()
    if not alerts.is_configured(s):
        sys.exit("Preencha SMTP_HOST, SMTP_USER, SMTP_PASSWORD e ALERT_EMAILS no .env.")
    with new_session() as db:
        msg = alerts.digest_message(alerts.build_digest(db, datetime.now(UTC)), s, datetime.now(UTC))
    msg.subject = "[TESTE] " + msg.subject
    try:
        alerts.send_email(s, msg, alerts.recipients(s))
    except alerts.AlertError as exc:
        sys.exit(f"{exc}\nNo Gmail, use uma 'senha de app' (não a senha normal da conta).")
    print(f"E-mail de teste enviado para: {', '.join(alerts.recipients(s))}")


if __name__ == "__main__":
    main()
