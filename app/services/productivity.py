# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Produtividade da bancada: conferências por dia e por hora, tempo por pedido, ponto e custo por pacote.

De onde vêm os números:
- Conferências: `returns.received_at` / `received_by` (quem confirmou) e `inspection_started_at` (quando abriu a
  devolução depois de bipar). Tempo por pedido = confirmar - abrir, em mediana (uma devolução esquecida aberta
  não distorce), sem contar mais de 30 min.
- Ponto (só para eficiência): eventos shift_start / pause / resume / shift_end em `operator_events`. Tempo em
  turno = do início ao fim, menos as pausas.
- Tempo ativo: bipagens, aberturas e conferências da pessoa. Atividades com até 10 min entre uma e outra formam
  um período ativo; mais que isso é parada.
- Custo por pacote = custo da hora da pessoa (Usuários) x horas em turno / conferências. Sem ponto no dia, usa o
  tempo ativo no lugar do turno.
"""

import re
import statistics
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import OperatorEvent, Return, User

IDLE_GAP = timedelta(minutes=10)  # sem atividade por mais que isso = parado
STOP_SHOWN = timedelta(minutes=20)  # paradas que aparecem na linha do tempo
MAX_PER_ORDER = timedelta(minutes=30)  # acima disso a devolução ficou aberta esquecida: não conta no tempo
MIN_SEGMENT = timedelta(minutes=1)  # uma bipagem sozinha conta 1 min de atividade
SHIFT_KINDS = ("shift_start", "pause", "resume", "shift_end")
ACTIVITY_KINDS = ("scan", "open", "inspect")
EMAIL_RE = re.compile(r"<([^>]+)>")


def tz() -> ZoneInfo:
    return ZoneInfo(get_settings().timezone)


def day_bounds(start: date, end: date) -> tuple[datetime, datetime]:
    z = tz()
    return datetime.combine(start, time.min, z), datetime.combine(end + timedelta(days=1), time.min, z)


@dataclass
class Person:
    user_id: str | None
    name: str
    email: str | None
    hourly_cost: Decimal | None = None
    inspections: list[dict[str, Any]] = field(default_factory=list)
    activity: list[datetime] = field(default_factory=list)
    shift: timedelta = timedelta(0)
    pauses: timedelta = timedelta(0)
    on_shift: str | None = None  # situação agora: working | paused | None
    shift_since: datetime | None = None

    def metrics(self) -> dict[str, Any]:
        active = active_time(self.activity)
        n = len(self.inspections)
        durations = [i["seconds"] for i in self.inspections if i["seconds"] is not None]
        base = self.shift if self.shift > timedelta(0) else active
        hours = base.total_seconds() / 3600
        cost = (float(self.hourly_cost) * hours / n) if (self.hourly_cost and n and hours) else None
        times = sorted(i["at"] for i in self.inspections)
        return {
            "user_id": self.user_id, "name": self.name, "email": self.email,
            "inspections": n,
            "median_seconds": round(statistics.median(durations)) if durations else None,
            "mean_seconds": round(statistics.mean(durations)) if durations else None,
            "shift_seconds": int(self.shift.total_seconds()),
            "pause_seconds": int(self.pauses.total_seconds()),
            "active_seconds": int(active.total_seconds()),
            "utilization": (active / self.shift) if self.shift > timedelta(0) else None,
            "per_hour": round(n / hours, 1) if hours else None,
            "cost_per_package": round(cost, 2) if cost is not None else None,
            "hourly_cost": float(self.hourly_cost) if self.hourly_cost is not None else None,
            "first": times[0] if times else None, "last": times[-1] if times else None,
            "longest_stop": longest_gap(times),
            "on_shift": self.on_shift, "shift_since": self.shift_since,
        }


def active_time(moments: list[datetime]) -> timedelta:
    """Soma dos períodos com atividade (até 10 min entre uma ação e outra)."""
    if not moments:
        return timedelta(0)
    moments = sorted(moments)
    total, seg_start, last = timedelta(0), moments[0], moments[0]
    for m in moments[1:]:
        if m - last > IDLE_GAP:
            total += max(last - seg_start, MIN_SEGMENT)
            seg_start = m
        last = m
    return total + max(last - seg_start, MIN_SEGMENT)


def longest_gap(times: list[datetime]) -> dict[str, Any] | None:
    best = None
    for a, b in zip(times, times[1:]):
        if b - a > IDLE_GAP and (best is None or b - a > best[1] - best[0]):
            best = (a, b)
    return {"start": best[0], "end": best[1], "seconds": int((best[1] - best[0]).total_seconds())} if best else None


def shift_time(events: list[tuple[str, datetime]], until: datetime) -> tuple[timedelta, timedelta, str | None, datetime | None]:
    """(tempo em turno sem pausas, pausas, situação no fim, início do turno aberto) a partir dos eventos do ponto."""
    worked = paused = timedelta(0)
    state, since, shift_since = None, None, None
    for kind, at in sorted(events, key=lambda e: e[1]):
        if kind == "shift_start" and state is None:
            state, since, shift_since = "working", at, at
        elif kind == "pause" and state == "working":
            worked += at - since
            state, since = "paused", at
        elif kind == "resume" and state == "paused":
            paused += at - since
            state, since = "working", at
        elif kind == "shift_end" and state is not None:
            if state == "working":
                worked += at - since
            else:
                paused += at - since
            state, since, shift_since = None, None, None
    if state == "working":
        worked += max(until - since, timedelta(0))
    elif state == "paused":
        paused += max(until - since, timedelta(0))
    return worked, paused, state, shift_since


def _email(actor: str | None) -> str | None:
    m = EMAIL_RE.search(actor or "")
    return m.group(1).strip().lower() if m else None


def collect(db: Session, start: date, end: date, user_id: str | None = None) -> dict[str, Any]:
    """Tudo que a tela e a API mostram, para o período e (opcional) uma pessoa."""
    since, until = day_bounds(start, end)
    now = datetime.now(UTC)
    users = {str(u.id): u for u in db.scalars(select(User)).all()}
    by_email = {u.email.lower(): u for u in users.values()}
    people: dict[str, Person] = {}

    def person_for(uid: str | None, actor: str | None = None) -> Person:
        u = users.get(uid) if uid else by_email.get(_email(actor) or "")
        key = str(u.id) if u else (actor or "Sem nome")
        if key not in people:
            people[key] = Person(str(u.id) if u else None, u.name if u else (actor or "Sem nome"),
                                 u.email if u else _email(actor), u.hourly_cost if u else None)
        return people[key]

    rets = db.execute(
        select(Return.id, Return.platform, Return.external_order_id, Return.item_condition, Return.received_at,
               Return.received_by, Return.inspection_started_at)
        .where(Return.received_at >= since, Return.received_at < until, Return.received_by.is_not(None))
    ).all()
    for r in rets:
        p = person_for(None, r.received_by)
        secs = None
        if r.inspection_started_at and r.received_at > r.inspection_started_at:
            d = r.received_at - r.inspection_started_at
            secs = int(d.total_seconds()) if d <= MAX_PER_ORDER else None
            if d <= MAX_PER_ORDER:
                p.activity.append(r.inspection_started_at)
        p.activity.append(r.received_at)
        p.inspections.append({"id": str(r.id), "platform": r.platform, "order": r.external_order_id,
                              "condition": r.item_condition, "at": r.received_at, "seconds": secs})

    events = db.execute(
        select(OperatorEvent.user_id, OperatorEvent.kind, OperatorEvent.at)
        .where(OperatorEvent.at >= since, OperatorEvent.at < until)
    ).all()
    shift_events: dict[str, list] = defaultdict(list)
    for e in events:
        p = person_for(str(e.user_id))
        if e.kind in ACTIVITY_KINDS:
            p.activity.append(e.at)
        elif e.kind in SHIFT_KINDS:
            shift_events[str(e.user_id)].append((e.kind, e.at))
    for uid, evs in shift_events.items():
        p = person_for(uid)
        p.shift, p.pauses, p.on_shift, p.shift_since = shift_time(evs, min(until, now))

    if user_id:
        people = {k: v for k, v in people.items() if v.user_id == user_id}
    rows = sorted((p.metrics() for p in people.values()), key=lambda m: -m["inspections"])
    all_insp = [i | {"who": p.name} for p in people.values() for i in p.inspections]
    z = tz()
    per_hour: dict[int, int] = defaultdict(int)
    for i in all_insp:
        per_hour[i["at"].astimezone(z).hour] += 1
    durations = [i["seconds"] for i in all_insp if i["seconds"] is not None]
    times = sorted(i["at"] for i in all_insp)
    cond = defaultdict(int)
    for i in all_insp:
        cond[i["condition"] or "none"] += 1
    total_shift = sum(r["shift_seconds"] for r in rows)
    total_active = sum(r["active_seconds"] for r in rows)
    hours = (total_shift or total_active) / 3600
    return {
        "start": start, "end": end, "single_day": start == end,
        "total": len(all_insp),
        "median_seconds": round(statistics.median(durations)) if durations else None,
        "mean_seconds": round(statistics.mean(durations)) if durations else None,
        "per_hour_rate": round(len(all_insp) / hours, 1) if hours else None,
        "first": times[0] if times else None, "last": times[-1] if times else None,
        "longest_stop": longest_gap(times),
        "hours": [{"hour": h, "total": per_hour.get(h, 0)} for h in range(min(per_hour, default=8), max(per_hour, default=17) + 1)],
        "conditions": dict(cond),
        "people": rows,
        "slowest": sorted((i for i in all_insp if i["seconds"] is not None), key=lambda i: -i["seconds"])[:5],
        "timeline": _timeline(people, start) if start == end else None,
    }


def _timeline(people: dict[str, Person], day: date) -> dict[str, Any]:
    """Faixa do dia por pessoa: um traço por conferência e as paradas de mais de 20 min (em % do horário)."""
    z = tz()
    times = [i["at"].astimezone(z) for p in people.values() for i in p.inspections]
    if not times:
        return {"hours": [], "lanes": []}
    first_h = max(0, min(t.hour for t in times) - (1 if min(t.minute for t in times) < 10 else 0))
    last_h = min(24, max(t.hour for t in times) + 1)
    t0 = datetime.combine(day, time(first_h), z)
    span = (last_h - first_h) * 3600
    pos = lambda t: round((t.astimezone(z) - t0).total_seconds() / span * 100, 2)  # noqa: E731
    lanes = []
    for p in sorted(people.values(), key=lambda p: -len(p.inspections)):
        ts = sorted(i["at"] for i in p.inspections)
        stops = [{"left": pos(a), "width": round(pos(b) - pos(a), 2), "minutes": int((b - a).total_seconds() // 60)}
                 for a, b in zip(ts, ts[1:]) if b - a > STOP_SHOWN]
        lanes.append({"name": p.name, "count": len(ts), "marks": [pos(t) for t in ts], "stops": stops})
    return {"hours": list(range(first_h, last_h + 1)), "lanes": lanes}


def daily_rows(db: Session, start: date, end: date) -> list[dict[str, Any]]:
    """Por pessoa e por dia (para a API do sistema de eficiência e o Excel)."""
    out = []
    d = start
    while d <= end:
        for m in collect(db, d, d)["people"]:
            out.append({
                "date": d.isoformat(), "user_id": m["user_id"], "name": m["name"], "email": m["email"],
                "inspections": m["inspections"], "shift_minutes": round(m["shift_seconds"] / 60),
                "pause_minutes": round(m["pause_seconds"] / 60), "active_minutes": round(m["active_seconds"] / 60),
                "utilization": round(m["utilization"], 3) if m["utilization"] is not None else None,
                "packages_per_hour": m["per_hour"], "median_seconds_per_package": m["median_seconds"],
                "hourly_cost": m["hourly_cost"], "cost_per_package": m["cost_per_package"],
            })
        d += timedelta(days=1)
    return out


# ---------- registro (chamado pelas telas) ----------

def log_event(db: Session, user: User, kind: str, return_id=None, detail: str | None = None, commit: bool = True) -> None:
    db.add(OperatorEvent(user_id=user.id, kind=kind, return_id=return_id, detail=(detail or "")[:120] or None))
    if commit:
        db.commit()


def shift_state(db: Session, user: User) -> dict[str, Any]:
    """Situação do ponto de hoje para o topo da fila."""
    since, until = day_bounds(*(2 * [datetime.now(tz()).date()]))
    evs = db.execute(select(OperatorEvent.kind, OperatorEvent.at).where(
        OperatorEvent.user_id == user.id, OperatorEvent.at >= since, OperatorEvent.kind.in_(SHIFT_KINDS))).all()
    worked, paused, state, shift_since = shift_time([(k, a) for k, a in evs], datetime.now(UTC))
    last = max((a for _, a in evs), default=None)
    return {"state": state, "worked_seconds": int(worked.total_seconds()), "paused_seconds": int(paused.total_seconds()),
            "since": shift_since, "last_change": last}


NEXT = {None: ("shift_start",), "working": ("pause", "shift_end"), "paused": ("resume", "shift_end")}


def punch(db: Session, user: User, kind: str) -> None:
    state = shift_state(db, user)["state"]
    if kind not in NEXT[state]:
        raise ValueError("Ação de ponto inválida agora")
    log_event(db, user, kind)
