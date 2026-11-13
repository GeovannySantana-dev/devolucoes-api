# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Aviso ao gestor quando uma integração para de funcionar (token vencido, loja sem sincronizar, rotina com erro).

Roda no fim das rotinas, por e-mail para ADMIN_EMAILS (ou ALERT_EMAILS, se ADMIN_EMAILS estiver vazio).
Loja parada ou autorização vencendo: um e-mail quando começa e outro quando a loja volta (alerts_sent
"open:<problema>"). Os demais problemas (rotina com erro, IP mudou) são avisados uma vez por dia.
"""

import uuid
from datetime import UTC, datetime, timedelta
from html import escape
from zoneinfo import ZoneInfo

import httpx
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import Settings, get_settings
from ..models import AlertSent, Return, Store
from . import alerts, store_status

PLATFORM = alerts.PLATFORM
LAST_IP_KEY = "public_ip:last"  # último IP de internet visto (guardado em alerts_sent.summary)


def check_public_ip(db: Session, settings: Settings | None = None, fetch=None) -> dict:
    """Confere o IP de internet do servidor e guarda o último visto. Se mudou, o health avisa por e-mail."""
    s = settings or get_settings()
    if not s.public_ip_url:
        return {"ip": "não confere"}
    if fetch is None:
        fetch = lambda: httpx.get(s.public_ip_url, timeout=10).raise_for_status().text  # noqa: E731
    try:
        ip = fetch().strip()
    except httpx.HTTPError:
        return {"ip": "não consegui consultar"}  # internet instável: tenta de novo na próxima rodada
    row = db.get(AlertSent, LAST_IP_KEY)
    if row is None:
        db.add(AlertSent(key=LAST_IP_KEY, summary=ip))
        db.commit()
        return {"ip": ip}
    old = row.summary
    if old == ip:
        return {"ip": ip}
    row.summary = ip
    db.commit()
    return {"ip": ip, "changed_from": old}


def find_problems(db: Session, results: dict, now: datetime) -> list[tuple[str, str]]:
    """(chave, descrição) de cada problema encontrado nesta rodada."""
    problems: list[tuple[str, str]] = []
    for step, r in results.items():
        if not r.get("ok", True):
            problems.append((f"step:{step}", f"Rotina \"{step}\" falhou: {r.get('error')}"))
    ip = (results.get("ip") or {}).get("result") or {}
    if ip.get("changed_from"):
        problems.append((
            f"ip:{ip['ip']}",
            f"O IP de internet do servidor mudou de {ip['changed_from']} para {ip['ip']}. Adicione {ip['ip']} na lista "
            "branca de IPs do aplicativo no Open Platform da Shein, senão a Shein para de sincronizar",
        ))
    copy = (results.get("copia_midias") or {}).get("result") or {}
    if copy.get("cheio"):
        problems.append(("copia_midias:cheio", "Cópia das fotos no Supabase parou: o espaço do plano acabou. "
                         "Assine o plano pago ou aumente MEDIA_BACKUP_MAX_GB"))
    # Loja parada: o motivo vem da última falha guardada pela sincronização (store.last_sync_error)
    for store in db.scalars(select(Store).where(Store.active, Store.access_token.is_not(None))).all():
        plat = PLATFORM.get(store.platform, store.platform)
        name = plat if store.label == plat else f"{plat} · {store.label}"
        st = store_status.status(store, now)
        if st.level == "bad":
            since = store.sync_error_since or store.last_synced_at
            when = f" desde {since.astimezone(ZoneInfo(get_settings().timezone)):%d/%m às %H:%M}" if since else ""
            problems.append((f"store:{store.id}", f"{name}: parada{when}. Motivo: {st.detail}"))
        if store.refresh_token_expires_at and store.refresh_token_expires_at - now < timedelta(days=3):
            problems.append(
                (f"token:{store.id}", f"{name}: autorização vence em {store.refresh_token_expires_at:%d/%m}; autorize a loja de novo")
            )
    return problems


def _recipients(s: Settings) -> list[str]:
    raw = s.admin_emails or s.alert_emails
    return [e.strip() for e in raw.split(",") if e.strip()]


OPEN = ("store:", "token:")  # avisados uma vez até resolver, não todo dia


def _sent_key(day: str, key: str) -> str:
    return f"open:{key}" if key.startswith(OPEN) else f"health:{day}:{key}"


def _problem_message(new: list[tuple[str, str]], url: str) -> alerts.Message:
    stores = [d for k, d in new if k.startswith("store:")]
    if len(new) == 1 and stores:
        name = stores[0].split(":", 1)[0]
        subject = f"⛔ {name} parada: devoluções novas não estão entrando"
        lead = (f"<p>{escape(stores[0])}</p><p>Enquanto isso, as devoluções novas da loja não aparecem na fila.</p>"
                "<p style='background:#f6f6f8;border-radius:8px;padding:10px 12px'><b>O que fazer:</b> abra Integrações e "
                "lojas no app e toque em <b>Autorizar de novo</b>, logado no painel da plataforma.</p>")
    else:
        subject = f"⚠ Devoluções: {len(new)} problema(s) nas integrações"
        lead = "<ul>" + "".join(f"<li style='margin:6px 0'>{escape(d)}</li>" for _, d in new) + "</ul>"
    html = (
        "<div style='font-family:system-ui,Segoe UI,Roboto,sans-serif;max-width:620px'>"
        f"<h2>{escape(subject)}</h2>{lead}"
        f"<p><a href='{url}/app/integracoes#lojas' style='display:inline-block;background:#0f6e56;color:#fff;"
        "text-decoration:none;padding:10px 16px;border-radius:8px;font-weight:600'>Abrir Integrações e lojas</a></p>"
        "<p style='color:#6b645e;font-size:13px'>Loja parada: você recebe este aviso uma vez e outro quando ela voltar.</p></div>"
    )
    text = subject + "\n\n" + "\n".join(f"- {d}" for _, d in new) + f"\n\nIntegrações: {url}/app/integracoes#lojas\n"
    return alerts.Message(subject, text, html)


def _resolved(db: Session, current: set[str], now: datetime) -> list[tuple[AlertSent, str]]:
    """Lojas que estavam paradas (aviso aberto) e voltaram: (aviso, texto do e-mail de volta)."""
    out = []
    for row in db.scalars(select(AlertSent).where(AlertSent.key.like("open:store:%"))).all():
        key = row.key.removeprefix("open:")
        if key in current:
            continue
        store = db.get(Store, uuid.UUID(key.removeprefix("store:")))
        name = (row.summary or "").split(":", 1)[0] or "A loja"
        new = 0
        if store is not None:
            new = db.scalar(select(func.count()).select_from(Return)
                            .where(Return.store_id == store.id, Return.created_at >= row.sent_at)) or 0
        out.append((row, f"{name} voltou a sincronizar depois de {store_status.since_text(now - row.sent_at)} de aviso. "
                         f"Na volta entraram {new} devolução(ões) nova(s) na fila."))
    return out


def check_health(db: Session, results: dict, now: datetime | None = None, settings: Settings | None = None) -> dict:
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    problems = find_problems(db, results, now)
    to = _recipients(s)
    configured = bool(s.smtp_host and s.smtp_user and s.smtp_password and to)
    url = (s.app_url or s.public_base_url).rstrip("/")
    resolved = _resolved(db, {k for k, _ in problems}, now)
    for row, text in resolved:
        if configured:
            subject = f"✅ {text.split(' voltou', 1)[0]} voltou a sincronizar"
            html = f"<div style='font-family:system-ui,Segoe UI,Roboto,sans-serif;max-width:620px'><h2>{escape(subject)}</h2><p>{escape(text)}</p></div>"
            alerts.send_email(s, alerts.Message(subject, text, html), to)
        db.delete(row)
    if resolved:
        db.commit()
    if not problems:
        return {"problems": 0, **({"resolved": len(resolved)} if resolved else {})}
    if not configured:
        return {"problems": len(problems), "skipped": "e-mail não configurado"}

    day = now.astimezone(ZoneInfo(s.timezone)).strftime("%Y-%m-%d")
    new = [(k, d) for k, d in problems if db.get(AlertSent, _sent_key(day, k)) is None]
    if not new:
        return {"problems": len(problems), "new": 0}
    alerts.send_email(s, _problem_message(new, url), to)
    for k, d in new:
        db.add(AlertSent(key=_sent_key(day, k), recipients=", ".join(to), summary=d[:500]))
    db.commit()
    return {"problems": len(problems), "new": len(new)}
