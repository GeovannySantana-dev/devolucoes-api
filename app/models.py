import uuid
from datetime import datetime
from decimal import Decimal
from typing import Any

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    SmallInteger,
    Text,
    UniqueConstraint,
    func,
)
from sqlalchemy.dialects.postgresql import JSONB, UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from .crypto import EncryptedText
from .db import Base


def _uuid_pk() -> Mapped[uuid.UUID]:
    return mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)


def _created_at() -> Mapped[datetime]:
    return mapped_column(DateTime(timezone=True), server_default=func.now())


class Store(Base):
    __tablename__ = "stores"
    __table_args__ = (UniqueConstraint("platform", "external_shop_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    platform: Mapped[str] = mapped_column(Text)
    name: Mapped[str] = mapped_column(Text)
    display_name: Mapped[str | None] = mapped_column(Text)  # nome escolhido pela equipe
    last_sync_error: Mapped[str | None] = mapped_column(Text)  # motivo da última falha (limpa quando volta)
    sync_error_since: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    external_shop_id: Mapped[str | None] = mapped_column(Text)
    shop_cipher: Mapped[str | None] = mapped_column(Text)
    tiktok_app: Mapped[int] = mapped_column(SmallInteger, default=1)  # qual app privado do TikTok autorizou a loja
    region: Mapped[str | None] = mapped_column(Text)
    erp_ecommerce_id: Mapped[str | None] = mapped_column(Text)
    # Cifrados no banco quando TOKEN_ENCRYPTION_KEY está definida (app/crypto.py)
    access_token: Mapped[str | None] = mapped_column(EncryptedText)
    refresh_token: Mapped[str | None] = mapped_column(EncryptedText)
    access_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    refresh_token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_synced_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    @property
    def label(self) -> str:
        """Nome mostrado nas telas e relatórios: o escolhido pela equipe ou o da plataforma."""
        return (self.display_name or "").strip() or self.name

    @property
    def authorized(self) -> bool:
        return bool(self.access_token and self.refresh_token)


class RejectionReason(Base):
    """Catálogo interno de motivos de recusa, usado nos relatórios."""

    __tablename__ = "rejection_reasons"

    code: Mapped[str] = mapped_column(Text, primary_key=True)
    label: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = _created_at()


class Return(Base):
    __tablename__ = "returns"
    __table_args__ = (UniqueConstraint("store_id", "external_return_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    store_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("stores.id", ondelete="CASCADE"))
    platform: Mapped[str] = mapped_column(Text)
    kind: Mapped[str] = mapped_column(Text, default="customer_return")
    external_return_id: Mapped[str] = mapped_column(Text)
    external_order_id: Mapped[str | None] = mapped_column(Text)
    return_type: Mapped[str] = mapped_column(Text)
    status: Mapped[str] = mapped_column(Text)
    platform_status: Mapped[str | None] = mapped_column(Text)
    reason_code: Mapped[str | None] = mapped_column(Text)
    reason_text: Mapped[str | None] = mapped_column(Text)
    refund_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    currency: Mapped[str | None] = mapped_column(Text)
    tracking_number: Mapped[str | None] = mapped_column(Text)
    carrier: Mapped[str | None] = mapped_column(Text)
    requested_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    platform_updated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    action_deadline: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    decision: Mapped[str | None] = mapped_column(Text)
    decided_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    decided_by: Mapped[str | None] = mapped_column(Text)
    platform_reject_reason_code: Mapped[str | None] = mapped_column(Text)
    platform_reject_reason_text: Mapped[str | None] = mapped_column(Text)
    internal_reject_reason_code: Mapped[str | None] = mapped_column(
        ForeignKey("rejection_reasons.code")
    )
    reject_comment: Mapped[str | None] = mapped_column(Text)
    internal_notes: Mapped[str | None] = mapped_column(Text)

    # Conferência física e condição do item
    item_condition: Mapped[str | None] = mapped_column(Text)
    condition_source: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    received_by: Mapped[str | None] = mapped_column(Text)
    # Câmera da bancada: abertura da conferência e recorte do vídeo
    inspection_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    camera_clip_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    camera_clip_error: Mapped[str | None] = mapped_column(Text)
    inspection_notes: Mapped[str | None] = mapped_column(Text)
    defect_reason: Mapped[str | None] = mapped_column(Text)  # motivo do defeito (perda, CFOP 5.927)
    buyer_key: Mapped[str | None] = mapped_column(Text)  # comprador (Shopee/ML), para avisar reincidência

    # Tratada fora do sistema (processo antigo): sai da fila de conferência, sem NF nem estoque
    handled_outside_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    handled_outside_by: Mapped[str | None] = mapped_column(Text)

    # Logística reversa (rastreio do pacote de volta, ver app/services/tracking.py)
    logistics_status: Mapped[str | None] = mapped_column(Text)
    logistics_detail: Mapped[str | None] = mapped_column(Text)
    logistics_event_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    logistics_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    # Olist ERP
    erp_order_id: Mapped[str | None] = mapped_column(Text)
    erp_order_number: Mapped[str | None] = mapped_column(Text)
    erp_sale_invoice_id: Mapped[str | None] = mapped_column(Text)
    erp_sale_invoice_key: Mapped[str | None] = mapped_column(Text)
    sale_tracking_number: Mapped[str | None] = mapped_column(Text)  # rastreio do envio da venda (Olist)
    erp_return_invoice_id: Mapped[str | None] = mapped_column(Text)
    erp_return_invoice_number: Mapped[str | None] = mapped_column(Text)
    erp_return_invoice_status: Mapped[str | None] = mapped_column(Text)
    erp_stock_status: Mapped[str | None] = mapped_column(Text)
    erp_stock_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    erp_last_error: Mapped[str | None] = mapped_column(Text)
    # Lote de transferência matriz (SP) -> filial (MS) em que a peça de revenda foi enviada
    transfer_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transfer_batches.id", ondelete="SET NULL"))
    # Lote de volta MS -> SP em que a peça saiu da filial e voltou para venda na matriz
    back_batch_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("transfer_batches.id", ondelete="SET NULL"))

    # Custo para a empresa (ver app/services/costs.py)
    liability: Mapped[str | None] = mapped_column(Text)
    cost_return_shipping: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    cost_product_loss: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    cost_operational: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    cost_lost_sale: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)  # frete de ida + taxas perdidos
    cost_total: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    cost_computed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Extrato financeiro do TikTok (custo confirmado); vazios = custo estimado
    settled_return_shipping: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    settled_lost_sale: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    cost_settled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cost_settle_checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now()
    )

    store: Mapped[Store] = relationship()
    items: Mapped[list["ReturnItem"]] = relationship(
        back_populates="ret", cascade="all, delete-orphan"
    )
    events: Mapped[list["ReturnEvent"]] = relationship(
        back_populates="ret", cascade="all, delete-orphan", order_by="ReturnEvent.created_at"
    )
    media: Mapped[list["ReturnMedia"]] = relationship(
        cascade="all, delete-orphan", order_by="ReturnMedia.created_at"
    )


class ReturnItem(Base):
    __tablename__ = "return_items"

    id: Mapped[uuid.UUID] = _uuid_pk()
    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("returns.id", ondelete="CASCADE"))
    external_item_id: Mapped[str | None] = mapped_column(Text)
    sku_id: Mapped[str | None] = mapped_column(Text)
    seller_sku: Mapped[str | None] = mapped_column(Text)
    product_name: Mapped[str | None] = mapped_column(Text)
    sku_name: Mapped[str | None] = mapped_column(Text)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    refund_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    image_url: Mapped[str | None] = mapped_column(Text)
    raw: Mapped[dict[str, Any] | None] = mapped_column(JSONB)

    ret: Mapped[Return] = relationship(back_populates="items")


class ReturnEvent(Base):
    """Histórico/auditoria de tudo que aconteceu com a devolução."""

    __tablename__ = "return_events"

    id: Mapped[uuid.UUID] = _uuid_pk()
    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("returns.id", ondelete="CASCADE"))
    event_type: Mapped[str] = mapped_column(Text)
    source: Mapped[str | None] = mapped_column(Text)  # sync | webhook | api
    from_status: Mapped[str | None] = mapped_column(Text)
    to_status: Mapped[str | None] = mapped_column(Text)
    actor: Mapped[str | None] = mapped_column(Text)
    message: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    created_at: Mapped[datetime] = _created_at()

    ret: Mapped[Return] = relationship(back_populates="events")


class ReturnReasonRule(Base):
    """Palavra-chave no motivo do comprador -> condição sugerida (resale/defect)."""

    __tablename__ = "return_reason_rules"

    keyword: Mapped[str] = mapped_column(Text, primary_key=True)
    condition: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=100)


class User(Base):
    """Usuário das telas da equipe (login individual)."""

    __tablename__ = "users"

    id: Mapped[uuid.UUID] = _uuid_pk()
    email: Mapped[str] = mapped_column(Text, unique=True)
    name: Mapped[str] = mapped_column(Text)
    role: Mapped[str] = mapped_column(Text)
    password_hash: Mapped[str] = mapped_column(Text)
    active: Mapped[bool] = mapped_column(Boolean, default=True)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    hourly_cost: Mapped[Decimal | None] = mapped_column(Numeric(10, 2))  # salário + encargos, por hora
    session_version: Mapped[int] = mapped_column(Integer, default=0)  # +1 = sai de todos os aparelhos
    created_at: Mapped[datetime] = _created_at()

    @property
    def actor(self) -> str:
        """Como a pessoa aparece no histórico das devoluções."""
        return f"{self.name} <{self.email}>"


class ReturnMedia(Base):
    """Foto ou vídeo de uma devolução (conferência ou enviado pelo comprador)."""

    __tablename__ = "return_media"

    id: Mapped[uuid.UUID] = _uuid_pk()
    return_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("returns.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)
    source: Mapped[str] = mapped_column(Text, default="inspection")
    storage_path: Mapped[str] = mapped_column(Text)
    content_type: Mapped[str | None] = mapped_column(Text)
    size_bytes: Mapped[int | None] = mapped_column(BigInteger)
    original_name: Mapped[str | None] = mapped_column(Text)
    uploaded_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    compressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # vídeo comprimido no servidor
    original_size_bytes: Mapped[int | None] = mapped_column(BigInteger)  # tamanho antes da compressão
    backup_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))  # copiado para o Supabase Storage


class AlertSent(Base):
    """Aviso por e-mail já enviado (evita mandar o mesmo resumo/alerta duas vezes)."""

    __tablename__ = "alerts_sent"

    key: Mapped[str] = mapped_column(Text, primary_key=True)
    sent_at: Mapped[datetime] = _created_at()
    recipients: Mapped[str | None] = mapped_column(Text)
    summary: Mapped[str | None] = mapped_column(Text)


class ApiKey(Base):
    """Chave de integração somente leitura (/integracao/v1). Só o hash fica guardado."""

    __tablename__ = "api_keys"

    id: Mapped[uuid.UUID] = _uuid_pk()
    name: Mapped[str] = mapped_column(Text)
    prefix: Mapped[str] = mapped_column(Text)
    key_hash: Mapped[str] = mapped_column(Text, unique=True)
    created_by: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class WebhookEvent(Base):
    __tablename__ = "webhook_events"
    __table_args__ = (UniqueConstraint("platform", "notification_id"),)

    id: Mapped[uuid.UUID] = _uuid_pk()
    platform: Mapped[str] = mapped_column(Text)
    store_id: Mapped[uuid.UUID | None] = mapped_column(
        ForeignKey("stores.id", ondelete="SET NULL")
    )
    notification_id: Mapped[str | None] = mapped_column(Text)
    event_type: Mapped[str | None] = mapped_column(Text)
    external_id: Mapped[str | None] = mapped_column(Text)
    payload: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime] = _created_at()


class ProductCostModel(Base):
    """Custo da peça por modelo (planilha "Custo e Produção"), ligado ao SKU pelo início dele."""

    __tablename__ = "product_cost_models"

    sku_prefix: Mapped[str] = mapped_column(Text, primary_key=True)
    model_code: Mapped[str] = mapped_column(Text)
    model_name: Mapped[str] = mapped_column(Text)
    unit_cost: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class TransferBatch(Base):
    """Lote de transferência das peças de revenda entre a matriz (SP, conta 1 do Olist) e a filial (MS, conta 2).

    Ida (to_ms): as peças conferidas saem do depósito de devoluções de SP e entram no MS. Volta (to_sp): as peças
    bipadas no MS saem da filial e entram no depósito de venda de SP.
    """

    __tablename__ = "transfer_batches"

    id: Mapped[uuid.UUID] = _uuid_pk()
    number: Mapped[int] = mapped_column(Integer, server_default=func.nextval("transfer_batches_number_seq"))
    # Ida (to_ms): closed | invoiced | stocked | error. Volta (to_sp): open | closed | invoiced | shipped | stocked | error
    status: Mapped[str] = mapped_column(Text, default="closed")
    direction: Mapped[str] = mapped_column(Text, default="to_ms")  # to_ms (SP -> MS) | to_sp (MS -> SP)
    closed_at: Mapped[datetime] = _created_at()
    closed_by: Mapped[str | None] = mapped_column(Text)
    pieces: Mapped[int] = mapped_column(Integer, default=0)
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2), default=0)
    erp_invoice_id: Mapped[str | None] = mapped_column(Text)
    erp_invoice_number: Mapped[str | None] = mapped_column(Text)
    erp_invoice_status: Mapped[str | None] = mapped_column(Text)
    ms_stock_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    ms_stock_result: Mapped[dict[str, Any] | None] = mapped_column(JSONB)
    sp_stock_posted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    created_at: Mapped[datetime] = _created_at()

    returns: Mapped[list[Return]] = relationship(foreign_keys="Return.transfer_batch_id")
    back_returns: Mapped[list[Return]] = relationship(foreign_keys="Return.back_batch_id")


class OperatorEvent(Base):
    """O que cada operador fez e quando: bipagem, abrir e conferir devolução, e o ponto do turno (produtividade)."""

    __tablename__ = "operator_events"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    user_id: Mapped[uuid.UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"))
    kind: Mapped[str] = mapped_column(Text)  # shift_start | pause | resume | shift_end | scan | open | inspect
    return_id: Mapped[uuid.UUID | None] = mapped_column(ForeignKey("returns.id", ondelete="SET NULL"))
    detail: Mapped[str | None] = mapped_column(Text)
    at: Mapped[datetime] = _created_at()
