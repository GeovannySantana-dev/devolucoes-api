from enum import StrEnum


class Platform(StrEnum):
    TIKTOK = "tiktok"
    SHOPEE = "shopee"
    MERCADO_LIVRE = "mercado_livre"
    SHEIN = "shein"


class ReturnStatus(StrEnum):
    """Status unificado entre plataformas. O status original fica em `platform_status`."""

    PENDING_REVIEW = "pending_review"  # aguardando aprovação/recusa do vendedor
    AWAITING_BUYER_SHIPMENT = "awaiting_buyer_shipment"  # aprovado, comprador ainda não enviou
    IN_TRANSIT = "in_transit"  # comprador enviou; vendedor confirma recebimento
    RECEIVED = "received"  # pacote recebido, aguardando inspeção/decisão
    COMPLETED = "completed"  # reembolso/troca concluído
    REJECTED = "rejected"
    DISPUTED = "disputed"  # contestada pelo vendedor, em análise pela plataforma
    CANCELLED = "cancelled"  # cancelado pelo comprador
    RETURNING_TO_SELLER = "returning_to_seller"  # não entregue, voltando pela transportadora
    UNKNOWN = "unknown"


# Status em que o vendedor pode aprovar ou recusar
ACTIONABLE_STATUSES = {ReturnStatus.PENDING_REVIEW, ReturnStatus.IN_TRANSIT, ReturnStatus.RECEIVED}
TERMINAL_STATUSES = {ReturnStatus.COMPLETED, ReturnStatus.REJECTED, ReturnStatus.CANCELLED}


class ReturnType(StrEnum):
    REFUND_ONLY = "refund_only"
    RETURN_AND_REFUND = "return_and_refund"
    REPLACEMENT = "replacement"
    UNKNOWN = "unknown"


class Decision(StrEnum):
    APPROVED = "approved"
    REJECTED = "rejected"


class ReturnKind(StrEnum):
    CUSTOMER_RETURN = "customer_return"  # devolução solicitada pelo comprador no marketplace
    UNDELIVERED = "undelivered"  # mercadoria não entregue, retornou pela transportadora


class ItemCondition(StrEnum):
    RESALE = "resale"  # tamanho/cor errado, arrependimento: volta ao estoque
    DEFECT = "defect"  # defeito/avaria: não volta ao estoque
    # Veio outro produto no pacote (ex.: tentativa de golpe): NF de devolução emitida normalmente,
    # sem lançar estoque; as fotos servem de prova para contestar no marketplace
    MISMATCH = "mismatch"


# Condições em que a peça vendida não volta ao estoque (custo de perda)
LOSS_CONDITIONS = {ItemCondition.DEFECT, ItemCondition.MISMATCH}


class EventType(StrEnum):
    CREATED = "created"
    STATUS_CHANGED = "status_changed"
    APPROVED = "approved"
    REJECTED = "rejected"
    ACTION_FAILED = "action_failed"
    NOTE = "note"
    TRACKING = "tracking"
    HANDLED_OUTSIDE = "handled_outside"
    MEDIA_ADDED = "media_added"
    MEDIA_REMOVED = "media_removed"
    RECEIVED = "received"
    CONDITION_SET = "condition_set"
    ERP_LINKED = "erp_linked"
    ERP_INVOICE_CREATED = "erp_invoice_created"
    ERP_INVOICE_FOUND = "erp_invoice_found"
    ERP_INVOICE_AUTHORIZED = "erp_invoice_authorized"
    ERP_STOCK_POSTED = "erp_stock_posted"
    ERP_STOCK_SKIPPED = "erp_stock_skipped"
    ERP_ERROR = "erp_error"
