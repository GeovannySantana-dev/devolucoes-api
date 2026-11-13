"""Avisos por e-mail: resumo nos horários configurados (padrão 8h e 14h) e alerta de prazo urgente.

Roda dentro das rotinas (POST /jobs/run ou scripts/rotinas.py, a cada 15 min). O que já foi enviado
fica em `alerts_sent`, então cada resumo sai uma vez por horário e cada alerta uma vez por prazo.
"""

import logging
import smtplib
import ssl
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from email.message import EmailMessage
from html import escape
from zoneinfo import ZoneInfo

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from ..config import Settings, get_settings
from ..enums import ReturnStatus
from ..models import AlertSent, Return

log = logging.getLogger(__name__)

PLATFORM = {"tiktok": "TikTok", "shopee": "Shopee", "mercado_livre": "Mercado Livre", "shein": "Shein"}
LIST_LIMIT = 15
# Mesma regra da fila "Aguardando decisão": só o que a loja consegue resolver agora.
# (No TikTok, "a caminho" tem prazo, mas a loja só decide quando o pacote chega.)
DECIDABLE = (ReturnStatus.PENDING_REVIEW, ReturnStatus.RECEIVED)


class AlertError(Exception):
    pass


@dataclass
class Message:
    subject: str
    text: str
    html: str


@dataclass
class Digest:
    overdue: list[Return] = field(default_factory=list)
    due_soon: list[Return] = field(default_factory=list)
    waiting: int = 0
    delivered_to_inspect: int = 0
    erp_errors: int = 0

    @property
    def empty(self) -> bool:
        return not (self.overdue or self.due_soon or self.waiting or self.delivered_to_inspect or self.erp_errors)


# ---------- configuração ----------

def recipients(s: Settings) -> list[str]:
    return [e.strip() for e in s.alert_emails.split(",") if e.strip()]


def is_configured(s: Settings) -> bool:
    return bool(s.smtp_host and s.smtp_user and s.smtp_password and recipients(s))


def _app_url(s: Settings) -> str:
    return (s.app_url or s.public_base_url).rstrip("/")


def send_email(s: Settings, msg: Message, to: list[str]) -> None:
    email = EmailMessage()
    email["Subject"] = msg.subject
    email["From"] = s.smtp_from or s.smtp_user
    email["To"] = ", ".join(to)
    email.set_content(msg.text)
    email.add_alternative(msg.html, subtype="html")
    password = s.smtp_password
    if "gmail.com" in s.smtp_host:
        password = password.replace(" ", "")  # o Google mostra a senha de app em blocos com espaço
    try:
        if s.smtp_port == 465:
            with smtplib.SMTP_SSL(s.smtp_host, s.smtp_port, context=ssl.create_default_context(), timeout=30) as smtp:
                smtp.login(s.smtp_user, password)
                smtp.send_message(email)
        else:
            with smtplib.SMTP(s.smtp_host, s.smtp_port, timeout=30) as smtp:
                smtp.starttls(context=ssl.create_default_context())
                smtp.login(s.smtp_user, password)
                smtp.send_message(email)
    except (smtplib.SMTPException, OSError) as exc:
        raise AlertError(f"Falha ao enviar e-mail: {exc}") from exc


# ---------- conteúdo ----------

def build_digest(db: Session, now: datetime) -> Digest:
    actionable = Return.status.in_(DECIDABLE)
    load = (selectinload(Return.items), selectinload(Return.store))
    overdue = db.scalars(
        select(Return).where(actionable, Return.action_deadline < now).options(*load).order_by(Return.action_deadline)
    ).all()
    due_soon = db.scalars(
        select(Return)
        .where(actionable, Return.action_deadline >= now, Return.action_deadline < now + timedelta(hours=24))
        .options(*load)
        .order_by(Return.action_deadline)
    ).all()
    count = lambda *conds: db.scalar(select(func.count()).select_from(Return).where(*conds)) or 0  # noqa: E731
    return Digest(
        overdue=list(overdue),
        due_soon=list(due_soon),
        waiting=count(Return.status.in_(DECIDABLE)),
        delivered_to_inspect=count(
            Return.received_at.is_(None),
            Return.handled_outside_at.is_(None),
            Return.status.not_in((ReturnStatus.CANCELLED, ReturnStatus.REJECTED)),
            (Return.logistics_status == "delivered") | (Return.status == ReturnStatus.RECEIVED),
            Return.requested_at >= now - timedelta(days=30),
        ),
        erp_errors=count(Return.erp_last_error.is_not(None)),
    )


def _line(r: Return, tz: ZoneInfo) -> tuple[str, str]:
    item = r.items[0] if r.items else None
    what = (item.product_name if item and item.product_name else f"Pedido {r.external_order_id}")[:70]
    deadline = r.action_deadline.astimezone(tz).strftime("%d/%m %H:%M") if r.action_deadline else "—"
    store = r.store.label if r.store else ""
    return f"{PLATFORM.get(r.platform, r.platform)} · {store}", f"{what} — prazo {deadline}"


def _section_html(title: str, rows: list[Return], s: Settings, tz: ZoneInfo, color: str) -> str:
    if not rows:
        return ""
    items = "".join(
        f'<li style="margin:6px 0"><a href="{_app_url(s)}/app/devolucoes/{r.id}" style="color:#0F6E56">'
        f"{escape(_line(r, tz)[1])}</a><br><span style=\"color:#6b645e;font-size:13px\">{escape(_line(r, tz)[0])}</span></li>"
        for r in rows[:LIST_LIMIT]
    )
    more = f'<p style="color:#6b645e">… e mais {len(rows) - LIST_LIMIT}.</p>' if len(rows) > LIST_LIMIT else ""
    return f'<h3 style="color:{color};margin:18px 0 6px">{escape(title)} ({len(rows)})</h3><ul style="padding-left:18px;margin:0">{items}</ul>{more}'


def _section_text(title: str, rows: list[Return], s: Settings, tz: ZoneInfo) -> str:
    if not rows:
        return ""
    lines = [f"\n{title} ({len(rows)}):"]
    for r in rows[:LIST_LIMIT]:
        where, what = _line(r, tz)
        lines.append(f"- {what} [{where}] {_app_url(s)}/app/devolucoes/{r.id}")
    if len(rows) > LIST_LIMIT:
        lines.append(f"... e mais {len(rows) - LIST_LIMIT}.")
    return "\n".join(lines)


def digest_message(d: Digest, s: Settings, now: datetime) -> Message:
    tz = ZoneInfo(s.timezone)
    when = now.astimezone(tz).strftime("%d/%m %Hh")
    parts = []
    if d.overdue:
        parts.append(f"{len(d.overdue)} vencida(s)")
    if d.due_soon:
        parts.append(f"{len(d.due_soon)} vencendo em 24h")
    subject = f"Devoluções {when}: " + (", ".join(parts) if parts else "nada urgente")
    counts = [
        ("Aguardando decisão", d.waiting, "decidir"),
        ("Pacotes entregues para conferir", d.delivered_to_inspect, "conferir"),
        ("Erros no Olist", d.erp_errors, "erros"),
    ]
    tiles = "".join(
        f'<td style="padding:10px 14px;border:1px solid #e7e3df;border-radius:8px"><a href="{_app_url(s)}/app?aba={tab}" '
        f'style="color:#1c1917;text-decoration:none"><div style="font-size:12px;color:#6b645e">{escape(label)}</div>'
        f'<div style="font-size:22px;font-weight:700">{value}</div></a></td>'
        for label, value, tab in counts
    )
    html = (
        '<div style="font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#1c1917;max-width:620px">'
        f'<h2 style="margin:0 0 12px">Resumo das devoluções · {escape(when)}</h2>'
        f'<table style="border-spacing:8px 0;margin:0 -8px"><tr>{tiles}</tr></table>'
        + _section_html("⚠ Prazo vencido", d.overdue, s, tz, "#b91c1c")
        + _section_html("Vencendo nas próximas 24h", d.due_soon, s, tz, "#a16207")
        + f'<p style="margin-top:20px"><a href="{_app_url(s)}/app" style="color:#0F6E56">Abrir o app de devoluções</a></p></div>'
    )
    text = (
        f"Resumo das devoluções · {when}\n\n"
        + "\n".join(f"{label}: {value}" for label, value, _ in counts)
        + _section_text("PRAZO VENCIDO", d.overdue, s, tz)
        + _section_text("VENCENDO NAS PRÓXIMAS 24H", d.due_soon, s, tz)
        + f"\n\nAbrir o app: {_app_url(s)}/app\n"
    )
    return Message(subject, text, html)


def urgent_message(rows: list[Return], s: Settings) -> Message:
    tz = ZoneInfo(s.timezone)
    subject = (
        f"⚠ Devolução vence em menos de {s.alert_urgent_hours}h"
        if len(rows) == 1
        else f"⚠ {len(rows)} devoluções vencem em menos de {s.alert_urgent_hours}h"
    )
    html = (
        '<div style="font-family:system-ui,Segoe UI,Roboto,sans-serif;color:#1c1917;max-width:620px">'
        f"<h2 style=\"margin:0 0 8px\">{escape(subject)}</h2>"
        "<p>Responda no marketplace antes do prazo para evitar decisão automática contra a loja.</p>"
        + _section_html("Vencem em breve", rows, s, tz, "#b91c1c")
        + "</div>"
    )
    text = subject + "\n" + _section_text("Vencem em breve", rows, s, tz) + "\n"
    return Message(subject, text, html)


# ---------- agendamento ----------

def _already_sent(db: Session, key: str) -> bool:
    return db.get(AlertSent, key) is not None


def _mark_sent(db: Session, key: str, to: list[str], summary: str) -> None:
    db.add(AlertSent(key=key, recipients=", ".join(to), summary=summary[:500]))


def due_digest_key(s: Settings, now: datetime) -> str | None:
    """Chave do resumo que já deveria ter saído hoje (último horário passado), ou None antes do primeiro."""
    local = now.astimezone(ZoneInfo(s.timezone))
    hours = sorted(int(h) for h in s.alert_digest_hours.split(",") if h.strip().isdigit())
    passed = [h for h in hours if local.hour >= h]
    return f"digest:{local:%Y-%m-%d}:{passed[-1]:02d}" if passed else None


def run_alerts(db: Session, now: datetime | None = None, settings: Settings | None = None) -> dict:
    """Job: envia o resumo do horário (se ainda não saiu) e os alertas urgentes novos."""
    s = settings or get_settings()
    now = now or datetime.now(UTC)
    if not is_configured(s):
        return {"skipped": "SMTP_HOST/SMTP_USER/SMTP_PASSWORD/ALERT_EMAILS não configurados"}
    to = recipients(s)
    result = {"digest": None, "urgent": 0}

    key = due_digest_key(s, now)
    if key and not _already_sent(db, key):
        d = build_digest(db, now)
        msg = digest_message(d, s, now)
        send_email(s, msg, to)
        _mark_sent(db, key, to, msg.subject)
        db.commit()
        result["digest"] = msg.subject

    soon = db.scalars(
        select(Return)
        .where(
            Return.status.in_(DECIDABLE),
            Return.action_deadline >= now,
            Return.action_deadline < now + timedelta(hours=s.alert_urgent_hours),
        )
        .options(selectinload(Return.items), selectinload(Return.store))
        .order_by(Return.action_deadline)
    ).all()
    # Um alerta por devolução e prazo (se o prazo mudar, avisa de novo)
    new = [r for r in soon if not _already_sent(db, f"urgent:{r.id}:{r.action_deadline:%Y%m%d%H%M}")]
    if new:
        msg = urgent_message(new, s)
        send_email(s, msg, to)
        for r in new:
            _mark_sent(db, f"urgent:{r.id}:{r.action_deadline:%Y%m%d%H%M}", to, msg.subject)
        db.commit()
        result["urgent"] = len(new)
    return result
