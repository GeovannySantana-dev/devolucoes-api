# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Conferência física do pacote devolvido (usada pela API e pelas telas da equipe)."""

from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..enums import EventType, ItemCondition, ReturnKind, ReturnStatus, ReturnType
from ..models import Return, ReturnEvent, ReturnMedia
from . import erp
from .costs import compute_costs


# Motivos de defeito aceitos (os nomes para a tela ficam em ui/labels.py, DEFECT_REASON)
DEFECT_REASONS = ("tear", "stain", "seam", "used", "missing_part", "other")


class InspectionError(Exception):
    def __init__(self, message: str, status_code: int = 409):
        self.status_code = status_code
        super().__init__(message)


def _media_count(db: Session, ret: Return) -> int:
    return db.scalar(select(func.count()).select_from(ReturnMedia).where(ReturnMedia.return_id == ret.id)) or 0


def receive_return(
    db: Session,
    ret: Return,
    actor: str | None,
    condition: str,
    notes: str | None,
    create_invoice: bool = True,
    defect_reason: str | None = None,
) -> None:
    """Registra a conferência (revenda ou defeito) e, se pedido, gera a NF de devolução no Olist.

    Erros do Olist ficam em `erp_last_error` e no histórico, sem desfazer o recebimento.
    """
    if ret.return_type == ReturnType.REFUND_ONLY:
        raise InspectionError("Reembolso sem devolução física: não há item para conferir")
    if ret.received_at:
        raise InspectionError("Devolução já conferida; corrija a condição em vez de conferir de novo")
    if condition == ItemCondition.MISMATCH and not _media_count(db, ret):
        raise InspectionError(
            "Item diferente do pedido: tire pelo menos uma foto do que veio no pacote (é a prova para contestar)",
            422,
        )

    if ret.kind == ReturnKind.UNDELIVERED:
        # Não há solicitação no marketplace: o recebimento encerra o retorno
        ret.events.append(
            ReturnEvent(
                event_type=EventType.STATUS_CHANGED,
                source="api",
                actor=actor,
                from_status=ret.status,
                to_status=ReturnStatus.COMPLETED,
            )
        )
        ret.status = ReturnStatus.COMPLETED
        ret.resolved_at = datetime.now(UTC)

    ret.received_at = datetime.now(UTC)
    ret.received_by = actor
    ret.inspection_notes = notes
    ret.item_condition = condition
    ret.defect_reason = defect_reason if condition == ItemCondition.DEFECT else None
    ret.condition_source = "manual"
    compute_costs(ret)
    payload = {"condition": condition} | ({"defect_reason": ret.defect_reason} if ret.defect_reason else {})
    ret.events.append(
        ReturnEvent(
            event_type=EventType.RECEIVED, source="api", actor=actor, message=notes, payload=payload
        )
    )
    db.commit()

    if create_invoice:
        erp.process_received(db, ret, actor)


def check_defect_evidence(db: Session, ret: Return, reason: str | None, notes: str | None) -> None:
    """Defeito vira perda no IRPJ/CSLL (NF 5.927): o contador pede motivo e foto registrados sempre."""
    if reason not in DEFECT_REASONS:
        raise InspectionError("Defeito: escolha qual é o defeito (é o motivo da perda)", 422)
    if reason == "other" and not (notes or "").strip():
        raise InspectionError('Defeito "Outro": descreva o defeito nas observações', 422)
    if not _media_count(db, ret):
        raise InspectionError("Defeito: tire pelo menos uma foto do defeito (é a prova da perda)", 422)


def set_condition(db: Session, ret: Return, actor: str | None, condition: str) -> None:
    """Corrige a condição do item (antes de o estoque ser lançado)."""
    if ret.erp_stock_status:
        raise InspectionError("Estoque já processado no Olist; ajuste manualmente lá")
    if ret.erp_return_invoice_id and ret.erp_return_invoice_status != "cancelled":
        raise InspectionError(
            "NF de devolução já criada com a natureza da condição anterior; cancele-a no Olist primeiro"
        )
    previous = ret.item_condition
    ret.item_condition = condition
    ret.condition_source = "manual"
    compute_costs(ret)
    ret.events.append(
        ReturnEvent(
            event_type=EventType.CONDITION_SET, source="api", actor=actor, payload={"from": previous, "to": condition}
        )
    )
    db.commit()


def _not_inspected_before(cutoff: datetime, only_closed: bool = False) -> list:
    conds = [
        Return.received_at.is_(None),
        Return.handled_outside_at.is_(None),
        Return.return_type != ReturnType.REFUND_ONLY,
        Return.status.not_in((ReturnStatus.CANCELLED, ReturnStatus.REJECTED)),
        Return.requested_at < cutoff,
    ]
    if only_closed:
        # Só as que a plataforma já encerrou: o que ainda está a caminho continua na fila para ser conferido
        conds.append(Return.status == ReturnStatus.COMPLETED)
    return conds


def handled_outside_preview(db: Session, cutoff: datetime, only_closed: bool = False) -> list[tuple[str, int]]:
    """Quantas devoluções físicas não conferidas, solicitadas antes do corte, por marketplace."""
    rows = db.execute(
        select(Return.platform, func.count())
        .where(*_not_inspected_before(cutoff, only_closed))
        .group_by(Return.platform)
        .order_by(func.count().desc())
    ).all()
    return [(p, n) for p, n in rows]


def mark_handled_outside(db: Session, cutoff: datetime, actor: str | None, only_closed: bool = False) -> int:
    """Marca como tratadas no processo antigo: saem da fila de conferência, sem NF nem estoque."""
    now = datetime.now(UTC)
    count = 0
    for ret in db.scalars(select(Return).where(*_not_inspected_before(cutoff, only_closed))):
        ret.handled_outside_at = now
        ret.handled_outside_by = actor
        ret.events.append(
            ReturnEvent(
                event_type=EventType.HANDLED_OUTSIDE,
                source="api",
                actor=actor,
                message="Tratada fora do sistema (processo antigo): sem conferência, NF ou estoque por aqui",
                payload={"cutoff": cutoff.isoformat(), "only_closed": only_closed},
            )
        )
        count += 1
        if count % 200 == 0:
            db.flush()
    db.commit()
    return count


def undo_handled_outside(db: Session, ret: Return, actor: str | None) -> None:
    ret.handled_outside_at = None
    ret.handled_outside_by = None
    ret.events.append(
        ReturnEvent(event_type=EventType.NOTE, source="api", actor=actor, message="Voltou para a fila de conferência")
    )
    db.commit()


def add_note(db: Session, ret: Return, actor: str | None, note: str) -> None:
    ret.internal_notes = f"{ret.internal_notes}\n{note}" if ret.internal_notes else note
    ret.events.append(ReturnEvent(event_type=EventType.NOTE, source="api", actor=actor, message=note))
    db.commit()
