# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from pydantic import BaseModel, ConfigDict, Field

from .enums import ItemCondition, Platform


class ORM(BaseModel):
    model_config = ConfigDict(from_attributes=True)


# ---------- lojas ----------

class StoreOut(ORM):
    id: uuid.UUID
    platform: str
    name: str
    external_shop_id: str | None
    region: str | None
    erp_ecommerce_id: str | None
    active: bool
    authorized: bool
    access_token_expires_at: datetime | None
    last_synced_at: datetime | None


class StoreCreate(BaseModel):
    platform: Platform
    name: str
    external_shop_id: str | None = None
    erp_ecommerce_id: str | None = Field(None, description="ID da integração desta loja no Olist")


class StoreUpdate(BaseModel):
    name: str | None = None
    active: bool | None = None
    erp_ecommerce_id: str | None = None


# ---------- devoluções ----------

class ReturnItemOut(ORM):
    id: uuid.UUID
    sku_id: str | None
    seller_sku: str | None
    product_name: str | None
    sku_name: str | None
    quantity: int
    refund_amount: Decimal | None
    image_url: str | None


class ReturnEventOut(ORM):
    id: uuid.UUID
    event_type: str
    source: str | None
    from_status: str | None
    to_status: str | None
    actor: str | None
    message: str | None
    payload: dict[str, Any] | None
    created_at: datetime


class ReturnOut(ORM):
    id: uuid.UUID
    store_id: uuid.UUID
    platform: str
    kind: str
    external_return_id: str
    external_order_id: str | None
    return_type: str
    status: str
    platform_status: str | None
    reason_code: str | None
    reason_text: str | None
    refund_amount: Decimal | None
    currency: str | None
    tracking_number: str | None
    carrier: str | None
    logistics_status: str | None
    logistics_detail: str | None
    logistics_event_at: datetime | None
    requested_at: datetime | None
    platform_updated_at: datetime | None
    action_deadline: datetime | None
    resolved_at: datetime | None
    decision: str | None
    decided_at: datetime | None
    decided_by: str | None
    platform_reject_reason_code: str | None
    platform_reject_reason_text: str | None
    internal_reject_reason_code: str | None
    reject_comment: str | None
    internal_notes: str | None
    item_condition: str | None
    condition_source: str | None
    received_at: datetime | None
    received_by: str | None
    inspection_notes: str | None
    erp_order_id: str | None
    erp_order_number: str | None
    erp_sale_invoice_id: str | None
    erp_return_invoice_id: str | None
    erp_return_invoice_number: str | None
    erp_return_invoice_status: str | None
    erp_stock_status: str | None
    erp_stock_posted_at: datetime | None
    erp_last_error: str | None
    liability: str | None
    cost_return_shipping: Decimal
    cost_product_loss: Decimal
    cost_operational: Decimal
    cost_total: Decimal
    items: list[ReturnItemOut]


class ReturnDetailOut(ReturnOut):
    events: list[ReturnEventOut]


class ReturnPage(ORM):
    total: int
    page: int
    page_size: int
    items: list[ReturnOut]


class ApproveIn(BaseModel):
    actor: str | None = Field(None, description="Quem aprovou (nome/e-mail do operador)")
    note: str | None = None
    decision: str | None = Field(
        None, description="Sobrescreve a decisão enviada à plataforma (ex.: APPROVE_REFUND). Normalmente deixe vazio."
    )


class RejectIn(BaseModel):
    platform_reason_code: str = Field(..., description="Código obtido em GET /returns/{id}/reject-reasons")
    internal_reason_code: str | None = Field(None, description="Motivo interno para relatórios")
    comment: str | None = Field(None, max_length=500, description="Comentário enviado ao comprador/plataforma")
    actor: str | None = None
    decision: str | None = None


class NoteIn(BaseModel):
    note: str
    actor: str | None = None


class ReasonOut(BaseModel):
    code: str
    label: str


class RejectReasonsOut(BaseModel):
    platform: list[ReasonOut]
    internal: list[ReasonOut]


class RejectionReasonIn(ORM):
    code: str = Field(..., pattern=r"^[a-z0-9_]+$")
    label: str
    active: bool = True


class ReceiveIn(BaseModel):
    condition: ItemCondition = Field(
        ..., description="resale = volta ao estoque (tamanho/cor errado etc.); defect = não volta ao estoque"
    )
    actor: str | None = None
    notes: str | None = Field(None, description="Observações da conferência física")
    create_invoice: bool = Field(True, description="Gerar a NF de devolução no Olist em seguida")


class UndeliveredIn(BaseModel):
    store_id: uuid.UUID
    order_number: str = Field(..., description="Número do pedido no marketplace")
    actor: str | None = None
    notes: str | None = None


class ConditionIn(BaseModel):
    condition: ItemCondition
    actor: str | None = None


class ReasonRuleIn(ORM):
    keyword: str
    condition: ItemCondition
    priority: int = 100


class SyncResultOut(BaseModel):
    store_id: str
    store_name: str
    fetched: int
    created: int
    updated: int
    errors: list[str]
