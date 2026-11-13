"""Textos em português e formatação usados nas telas."""

from datetime import UTC, datetime
from decimal import Decimal
from zoneinfo import ZoneInfo

from ..config import get_settings

STATUS = {
    "pending_review": ("Aguardando decisão", "warn"),
    "awaiting_buyer_shipment": ("Aguardando envio do comprador", "muted"),
    "in_transit": ("A caminho", "info"),
    "received": ("Pacote entregue", "info"),
    "completed": ("Concluída", "ok"),
    "rejected": ("Recusada", "bad"),
    "disputed": ("Em mediação", "warn"),
    "cancelled": ("Cancelada", "muted"),
    "returning_to_seller": ("Voltando (não entregue)", "info"),
    "unknown": ("Status desconhecido", "muted"),
}

PLATFORM = {
    "tiktok": "TikTok",
    "shopee": "Shopee",
    "mercado_livre": "Mercado Livre",
    "shein": "Shein",
}

CONDITION = {
    "resale": "Revenda (volta ao estoque)",
    "defect": "Defeito (não volta ao estoque)",
    "mismatch": "Item diferente do pedido (não volta ao estoque)",
}

# Motivo do defeito (registrado para a baixa por perda, CFOP 5.927)
DEFECT_REASON = {
    "tear": "Rasgo/furo",
    "stain": "Mancha",
    "seam": "Costura",
    "used": "Peça usada",
    "missing_part": "Faltando parte",
    "other": "Outro",
}

RETURN_TYPE = {
    "refund_only": "Só reembolso (sem devolução física)",
    "return_and_refund": "Devolução com reembolso",
    "replacement": "Troca",
    "unknown": "—",
}

INVOICE = {
    None: "Não criada",
    "draft": "Rascunho no Olist",
    "authorized": "Autorizada",
    "cancelled": "Cancelada",
}

STOCK = {
    None: "Pendente",
    "posted": "Lançado no estoque",
    "reserved": "Saiu do estoque de venda, falta lançar a nota",
    "launched": "Nota lançada, falta entrada no depósito certo",
    "skipped_defect": "Não lançado (defeito)",
    "skipped_mismatch": "Não lançado (item diferente)",
    "skipped_no_product": "Não lançado (produto excluído no Olist)",
    "external": "Tratado no Olist (NF manual)",
}

LOGISTICS = {
    "awaiting_shipment": ("Aguardando postagem", "muted"),
    "in_transit": ("A caminho", "info"),
    "delivered": ("Entregue na loja", "ok"),
    "failed": ("Falha na entrega", "bad"),
    "lost": ("Extraviado", "bad"),
    "cancelled": ("Envio cancelado", "muted"),
    "none": ("Comprador ainda não postou", "muted"),
}

EVENT = {
    "created": "Devolução registrada",
    "status_changed": "Status alterado",
    "approved": "Aceita",
    "rejected": "Recusada",
    "action_failed": "Falha ao enviar ação",
    "note": "Nota interna",
    "received": "Pacote conferido",
    "condition_set": "Condição alterada",
    "media_added": "Mídia adicionada",
    "media_removed": "Mídia removida",
    "erp_linked": "Vinculada ao pedido do Olist",
    "erp_invoice_created": "NF de devolução criada",
    "erp_invoice_found": "NF de devolução encontrada no Olist",
    "erp_invoice_authorized": "NF autorizada",
    "erp_stock_posted": "Estoque lançado",
    "erp_stock_skipped": "Estoque não lançado",
    "erp_error": "Erro no Olist",
    "tracking": "Rastreio",
    "handled_outside": "Tratada fora do sistema",
}


def status_label(value: str | None) -> str:
    return STATUS.get(value or "unknown", (value or "—", "muted"))[0]


def status_tone(value: str | None) -> str:
    return STATUS.get(value or "unknown", ("", "muted"))[1]


def event_label(value: str | None) -> str:
    if value and value.startswith("legacy:"):
        return f"Sistema antigo: {value.split(':', 1)[1]}"
    return EVENT.get(value or "", value or "—")


def local_dt(value: datetime | None, fmt: str = "%d/%m/%Y %H:%M") -> str:
    if not value:
        return "—"
    return value.astimezone(ZoneInfo(get_settings().timezone)).strftime(fmt)


def money(value: Decimal | float | None) -> str:
    if value is None:
        return "—"
    s = f"{Decimal(str(value)):,.2f}"
    return "R$ " + s.replace(",", "X").replace(".", ",").replace("X", ".")


def size(value: int | None) -> str:
    if not value:
        return ""
    return f"{value / 1024 / 1024:.1f} MB" if value >= 1024 * 1024 else f"{value / 1024:.0f} KB"


_MONTHS = ["jan", "fev", "mar", "abr", "mai", "jun", "jul", "ago", "set", "out", "nov", "dez"]


def month_label(iso: str) -> str:
    """'2026-10-01' -> 'out/26'"""
    year, month = iso[:4], int(iso[5:7])
    return f"{_MONTHS[month - 1]}/{year[2:]}"


def pct(value: float | None) -> str:
    return "—" if value is None else f"{value * 100:.0f}%".replace(".", ",")


def seconds(value: float | None) -> str:
    """Tempo curto de uma conferência: 42 s, 1 min 05 s."""
    if value is None:
        return "—"
    value = int(round(value))
    return f"{value} s" if value < 60 else f"{value // 60} min {value % 60:02d} s"


def hours(value: float | None) -> str:
    """Duração de turno/pausa: 7h38, 45 min."""
    if not value:
        return "—"
    minutes = int(value // 60)
    return f"{minutes} min" if minutes < 60 else f"{minutes // 60}h{minutes % 60:02d}"


def due(value: datetime | None, now: datetime | None = None) -> str:
    """Prazo curto para o cartão da fila: "hoje 12:40", "amanhã 09:00" ou "05/11"."""
    if value is None:
        return "—"
    tz = ZoneInfo(get_settings().timezone)
    local = value.astimezone(tz)
    days = (local.date() - (now or datetime.now(UTC)).astimezone(tz).date()).days
    if days == 0:
        return f"hoje {local:%H:%M}"
    if days == 1:
        return f"amanhã {local:%H:%M}"
    return f"{local:%d/%m}"


def register(env) -> None:
    env.globals["defect_reasons"] = DEFECT_REASON
    env.filters.update(
        status_label=status_label,
        status_tone=status_tone,
        due=due,
        platform=lambda v: PLATFORM.get(v or "", v or "—"),
        condition=lambda v: CONDITION.get(v or "", "Não definida"),
        defect_reason=lambda v: DEFECT_REASON.get(v or "", "Sem motivo"),
        return_type=lambda v: RETURN_TYPE.get(v or "unknown", v),
        invoice=lambda v: INVOICE.get(v, v),
        stock=lambda v: STOCK.get(v, v),
        event_label=event_label,
        logistics=lambda v: LOGISTICS.get(v or "", (v or "—", "muted"))[0],
        logistics_tone=lambda v: LOGISTICS.get(v or "", ("", "muted"))[1],
        dt=local_dt,
        money=money,
        size=size,
        month=month_label,
        pct=pct,
        seconds=seconds,
        hours=hours,
    )
