# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Aprovação e recusa de devoluções, com auditoria."""

import logging
from datetime import UTC, datetime

from sqlalchemy.orm import Session

from ..enums import ACTIONABLE_STATUSES, Decision, EventType, Platform
from ..models import RejectionReason, Return, ReturnEvent
from ..platforms.base import PlatformError
from ..platforms.registry import get_adapter
from .sync import upsert_return

log = logging.getLogger(__name__)

class ActionError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        self.status_code = status_code
        super().__init__(message)


def can_contest_after_accept(ret: Return) -> bool:
    """A Shopee permite contestar mesmo depois de aceitar (Devolução Fácil aceita antes de o pacote chegar)."""
    return ret.platform == Platform.SHOPEE and (ret.platform_status or "").startswith("ACCEPTED")


def _check_actionable(ret: Return, contest: bool = False) -> None:
    if ret.status in ACTIONABLE_STATUSES or (contest and can_contest_after_accept(ret)):
        return
    raise ActionError(f"Devolução em status '{ret.status}' não aceita aprovação/recusa", 409)


def _refresh_from_platform(db: Session, ret: Return) -> None:
    # A ação já foi aceita pela plataforma; se a releitura falhar, a próxima sync/webhook corrige
    try:
        nr = get_adapter(db, ret.store).fetch_return(ret.store, ret.external_return_id)
        if nr:
            upsert_return(db, ret.store, nr, source="api")
    except PlatformError:
        log.warning("Não foi possível reler a devolução %s após a ação", ret.id, exc_info=True)


def _record_failure(db: Session, ret: Return, actor: str | None, action: str, exc: Exception) -> None:
    ret.events.append(
        ReturnEvent(event_type=EventType.ACTION_FAILED, source="api", actor=actor, message=f"{action}: {exc}")
    )
    db.commit()


def approve(db: Session, ret: Return, actor: str | None, note: str | None, decision: str | None) -> Return:
    _check_actionable(ret)
    adapter = get_adapter(db, ret.store)
    try:
        adapter.approve(ret.store, ret, decision)
    except PlatformError as exc:
        _record_failure(db, ret, actor, "approve", exc)
        raise ActionError(str(exc), 502) from exc

    now = datetime.now(UTC)
    ret.decision = Decision.APPROVED
    ret.decided_at = now
    ret.decided_by = actor
    ret.events.append(
        ReturnEvent(event_type=EventType.APPROVED, source="api", actor=actor, from_status=ret.status, message=note)
    )
    db.commit()
    _refresh_from_platform(db, ret)
    db.commit()
    return ret


def reject(
    db: Session,
    ret: Return,
    actor: str | None,
    platform_reason_code: str,
    internal_reason_code: str | None,
    comment: str | None,
    decision: str | None,
) -> Return:
    _check_actionable(ret, contest=True)

    if internal_reason_code and not db.get(RejectionReason, internal_reason_code):
        raise ActionError(f"Motivo interno '{internal_reason_code}' não cadastrado", 422)

    adapter = get_adapter(db, ret.store)
    reasons = {r.code: r.label for r in adapter.reject_reasons(ret.store, ret)}
    if platform_reason_code not in reasons:
        raise ActionError(
            f"Motivo '{platform_reason_code}' não é aceito pela plataforma para esta devolução. "
            f"Consulte GET /returns/{ret.id}/reject-reasons",
            422,
        )

    try:
        adapter.reject(ret.store, ret, platform_reason_code, comment, decision)
    except PlatformError as exc:
        _record_failure(db, ret, actor, "reject", exc)
        raise ActionError(str(exc), 502) from exc

    # Aviso do adaptador (ex.: recusa foi sem as fotos): fica no histórico e a tela mostra
    warning = getattr(adapter, "last_warning", None)
    ret.reject_warning = warning
    if warning:
        ret.events.append(ReturnEvent(event_type=EventType.NOTE, source="api", actor=actor, message=warning))

    ret.decision = Decision.REJECTED
    ret.decided_at = datetime.now(UTC)
    ret.decided_by = actor
    ret.platform_reject_reason_code = platform_reason_code
    ret.platform_reject_reason_text = reasons[platform_reason_code]
    ret.internal_reject_reason_code = internal_reason_code
    ret.reject_comment = comment
    ret.events.append(
        ReturnEvent(
            event_type=EventType.REJECTED,
            source="api",
            actor=actor,
            from_status=ret.status,
            message=reasons[platform_reason_code],
            payload={
                "platform_reason_code": platform_reason_code,
                "internal_reason_code": internal_reason_code,
                "comment": comment,
            },
        )
    )
    db.commit()
    _refresh_from_platform(db, ret)
    db.commit()
    return ret
