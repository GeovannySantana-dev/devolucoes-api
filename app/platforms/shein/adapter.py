"""Adaptador da Shein (Autooperação, Brasil): lista e detalhes das devoluções de clientes."""

from collections.abc import Iterator
from datetime import datetime, timedelta
from typing import TYPE_CHECKING

from ..base import NormalizedReturn, PlatformAdapter, PlatformError, PlatformRejectReason
from .client import SheinClient
from .mapper import BEIJING, map_return

if TYPE_CHECKING:
    from ...models import Return, Store

LIST = "/open-api/return-order/list"
DETAILS = "/open-api/return-order/details"
SIGN = "/open-api/return-order/sign-return-order"
WINDOW = timedelta(hours=47)  # a lista aceita no máximo 48 h por consulta
PAGE_SIZE = 30  # máximo da Shein
QUERY_BY_UPDATE = 3  # queryType 3 = data da última atualização (1/2 = criação)
NO_DECISION = "Na Shein a devolução é aceita automaticamente; a loja só confere e confirma o recebimento."


class SheinAdapter(PlatformAdapter):
    def __init__(self, client: SheinClient):
        self.client = client

    def _call(self, store: "Store", path: str, body: dict) -> dict | list | None:
        if not store.access_token or not store.refresh_token:
            raise PlatformError(f"Loja {store.name} sem autorização da Shein")
        return self.client.store_request(path, body, store.access_token, store.refresh_token)

    def _list(self, store: "Store", start: datetime, end: datetime) -> Iterator[str]:
        fmt = lambda d: d.astimezone(BEIJING).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        page = 1
        while True:
            info = self._call(store, LIST, {"page": page, "pageSize": PAGE_SIZE, "queryType": QUERY_BY_UPDATE,
                                             "startTime": fmt(start), "endTime": fmt(end)}) or {}
            rows = info.get("returnOrderList") or []
            for r in rows:
                yield r["returnOrderNo"]
            if len(rows) < PAGE_SIZE or page * PAGE_SIZE >= int(info.get("count") or 0):
                return
            page += 1

    def _details(self, store: "Store", numbers: list[str]) -> list[dict]:
        return self._call(store, DETAILS, {"returnOrderNoList": numbers}) or []

    def fetch_returns(self, store: "Store", updated_since: datetime, updated_until: datetime) -> Iterator[NormalizedReturn]:
        start = updated_since
        while start < updated_until:
            end = min(start + WINDOW, updated_until)
            numbers = list(dict.fromkeys(self._list(store, start, end)))
            for i in range(0, len(numbers), 20):  # detalhes em lotes
                for d in self._details(store, numbers[i:i + 20]):
                    yield map_return(d)
            start = end

    def fetch_return(self, store: "Store", external_return_id: str) -> NormalizedReturn | None:
        rows = self._details(store, [external_return_id])
        return map_return(rows[0]) if rows else None

    def confirm_receipt(self, store: "Store", ret: "Return") -> None:
        """Confirma na Shein que o pacote chegou (status 5, Recebido). Sem isso a Shein confirma sozinha depois."""
        goods = [int(g["goodsId"]) for g in (ret.raw or {}).get("returnGoodsInfoList") or [] if g.get("goodsId")]
        if not goods:
            raise PlatformError("Devolução sem os itens da Shein (goodsId); atualize a devolução e tente de novo")
        self._call(store, SIGN, {"returnOrderNo": ret.external_return_id, "goodsIdList": goods})

    def approve(self, store: "Store", ret: "Return", decision: str | None = None) -> None:
        raise PlatformError(NO_DECISION)

    def reject(self, store, ret, platform_reason_code, comment, decision=None) -> None:
        raise PlatformError(NO_DECISION)

    def reject_reasons(self, store: "Store", ret: "Return") -> list[PlatformRejectReason]:
        return []
