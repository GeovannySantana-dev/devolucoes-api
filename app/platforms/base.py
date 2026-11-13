# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Contrato comum que cada marketplace implementa.

O resto da API só conversa com `PlatformAdapter` e com os dados normalizados abaixo,
então adicionar Shopee, Mercado Livre ou Shein é só criar um novo adaptador.
"""

from abc import ABC, abstractmethod
from collections.abc import Iterator
from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from ..enums import ReturnStatus, ReturnType

if TYPE_CHECKING:
    from ..models import Return, Store


class PlatformError(Exception):
    """Erro retornado pela API do marketplace."""


class PlatformNotSupported(Exception):
    pass


@dataclass
class NormalizedItem:
    external_item_id: str | None
    sku_id: str | None
    seller_sku: str | None
    product_name: str | None
    sku_name: str | None
    quantity: int
    refund_amount: Decimal | None
    image_url: str | None
    raw: dict[str, Any]


@dataclass
class NormalizedReturn:
    external_return_id: str
    external_order_id: str | None
    return_type: ReturnType
    status: ReturnStatus
    platform_status: str | None
    reason_code: str | None
    reason_text: str | None
    refund_amount: Decimal | None
    currency: str | None
    requested_at: datetime | None
    platform_updated_at: datetime | None
    action_deadline: datetime | None
    tracking_number: str | None
    carrier: str | None
    raw: dict[str, Any]
    items: list[NormalizedItem] = field(default_factory=list)


@dataclass
class TokenSet:
    access_token: str
    access_token_expires_at: datetime
    refresh_token: str
    refresh_token_expires_at: datetime | None
    seller_name: str | None
    region: str | None
    granted_scopes: list[str] | None = None


@dataclass
class PlatformRejectReason:
    code: str
    label: str


class PlatformAdapter(ABC):
    @abstractmethod
    def fetch_returns(
        self, store: "Store", updated_since: datetime, updated_until: datetime
    ) -> Iterator[NormalizedReturn]: ...

    @abstractmethod
    def fetch_return(self, store: "Store", external_return_id: str) -> NormalizedReturn | None: ...

    @abstractmethod
    def approve(self, store: "Store", ret: "Return", decision: str | None = None) -> None: ...

    @abstractmethod
    def reject(
        self,
        store: "Store",
        ret: "Return",
        platform_reason_code: str,
        comment: str | None,
        decision: str | None = None,
    ) -> None: ...

    @abstractmethod
    def reject_reasons(self, store: "Store", ret: "Return") -> list[PlatformRejectReason]: ...
