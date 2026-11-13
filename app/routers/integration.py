# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""API de integração, somente leitura: quantidades e custos das devoluções para outros sistemas internos.

Autenticação: header `X-API-Key` com uma chave de integração (`pdv_...`), criada pelo gestor em
/app/integracoes. Essa chave só lê: não aceita, recusa, gera NF nem altera nada.

Datas (`inicio`/`fim`) filtram pela data da solicitação da devolução, no fuso da loja (America/Sao_Paulo).
"""

import uuid
from datetime import date, datetime, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import func, select
from sqlalchemy.orm import Session, defer, selectinload

from ..config import get_settings
from ..db import get_db
from ..models import Return, ReturnItem
from ..security import require_integration_key
from ..services import dashboard, productivity
from ..ui import labels
from . import reports
from .filters import date_range

router = APIRouter(
    prefix="/integracao/v1",
    tags=["Integração (somente leitura)"],
    dependencies=[Depends(require_integration_key)],
)

MAX_DAYS = 3 * 366


def _period(inicio: date | None, fim: date | None) -> tuple[date, date]:
    today = datetime.now(ZoneInfo(get_settings().timezone)).date()
    end = fim or today
    start = inicio or end - timedelta(days=29)
    if start > end:
        raise HTTPException(422, "inicio deve ser anterior ou igual a fim")
    if (end - start).days > MAX_DAYS:
        raise HTTPException(422, "Período máximo de 3 anos por consulta")
    return start, end


def _platform(plataforma: str | None) -> str | None:
    if plataforma and plataforma not in labels.PLATFORM:
        raise HTTPException(422, f"plataforma deve ser uma de: {', '.join(labels.PLATFORM)}")
    return plataforma or None


def _num(v: Decimal | None) -> float | None:
    return float(v) if v is not None else None


def _dt(v: datetime | None) -> str | None:
    return v.isoformat() if v else None


@router.get("/painel")
def panel(
    inicio: date | None = None,
    fim: date | None = None,
    plataforma: str | None = None,
    loja: uuid.UUID | None = None,
    agrupar: str = Query("day", pattern="^(day|week|month)$"),
    db: Session = Depends(get_db),
):
    """Tudo do painel de Relatórios de uma vez: indicadores (com o período anterior para comparar), evolução,
    marketplaces, motivos, condição na conferência e produtos. Padrão: últimos 30 dias."""
    start, end = _period(inicio, fim)
    return dashboard.dashboard_data(db, start, end, _platform(plataforma), loja, group=agrupar)


@router.get("/produtividade")
def productivity_by_day(
    inicio: date | None = None,
    fim: date | None = None,
    db: Session = Depends(get_db),
):
    """Produtividade da bancada por pessoa e por dia, para o sistema de eficiência do operador.

    Por linha: pacotes conferidos, minutos em turno (ponto do app, sem pausas), pausas, minutos ativos (bipando
    ou conferindo, até 10 min entre uma ação e outra), aproveitamento (ativo / turno), pacotes por hora, tempo
    mediano por pacote (s), custo da hora informado em Usuários e custo por pacote. Padrão: últimos 30 dias;
    máximo de 92 dias por consulta.
    """
    start, end = _period(inicio, fim)
    if (end - start).days > 91:
        raise HTTPException(422, "Período máximo de 92 dias por consulta")
    return {"inicio": start.isoformat(), "fim": end.isoformat(), "linhas": productivity.daily_rows(db, start, end)}


@router.get("/custos")
def costs(
    inicio: date | None = None,
    fim: date | None = None,
    plataforma: str | None = None,
    loja: uuid.UUID | None = None,
    agrupar_por: str = Query("month", pattern="^(reason|liability|month|store|sku|condition)$"),
    db: Session = Depends(get_db),
):
    """Custo para a empresa (frete de devolução + perda de peça + operacional) e reembolsado, no total e
    agrupado por mês, motivo, loja, SKU, condição ou responsável. Devoluções canceladas ficam de fora."""
    start, end = _period(inicio, fim)
    f = reports.Filters(date_from=start, date_to=end, platform=_platform(plataforma), store_id=loja)
    data = reports.costs(f=f, group_by=agrupar_por, db=db)
    return {"filtros": {"inicio": start.isoformat(), "fim": end.isoformat(), "plataforma": plataforma,
                        "loja": str(loja) if loja else None}, **data}


def _return_row(r: Return) -> dict:
    return {
        "id": str(r.id),
        "plataforma": r.platform,
        "plataforma_nome": labels.PLATFORM.get(r.platform, r.platform),
        "loja": {"id": str(r.store_id), "nome": r.store.label if r.store else None},
        "tipo": r.kind,  # customer_return | undelivered
        "id_devolucao_marketplace": r.external_return_id,
        "pedido": r.external_order_id,
        "modalidade": r.return_type,
        "status": r.status,
        "status_marketplace": r.platform_status,
        "motivo": r.reason_text or r.reason_code,
        "motivo_codigo": r.reason_code,
        "reembolso": _num(r.refund_amount),
        "moeda": r.currency or "BRL",
        "decisao": r.decision,
        "condicao": r.item_condition,  # resale | defect | mismatch | None (não conferida)
        "logistica": r.logistics_status,
        "rastreio": r.tracking_number,
        "nf_devolucao": {"numero": r.erp_return_invoice_number, "status": r.erp_return_invoice_status},
        "estoque": r.erp_stock_status,
        "custos": {
            "frete_devolucao": _num(r.cost_return_shipping) or 0.0,
            "perda_produto": _num(r.cost_product_loss) or 0.0,
            "operacional": _num(r.cost_operational) or 0.0,
            "venda_perdida": _num(r.cost_lost_sale) or 0.0,  # TikTok: frete de ida e taxas que não voltam
            "confirmado_extrato": r.cost_settled_at is not None,
            "total": _num(r.cost_total) or 0.0,
            "responsavel": r.liability,
        },
        "datas": {
            "solicitada": _dt(r.requested_at),
            "prazo": _dt(r.action_deadline),
            "decidida": _dt(r.decided_at),
            "recebida": _dt(r.received_at),
            "resolvida": _dt(r.resolved_at),
            "atualizada": _dt(r.updated_at),
        },
        "itens": [
            {
                "sku": i.seller_sku,
                "produto": i.product_name,
                "variacao": i.sku_name,
                "quantidade": i.quantity,
                "reembolso": _num(i.refund_amount),
            }
            for i in r.items
        ],
    }


@router.get("/devolucoes")
def returns_list(
    inicio: date | None = None,
    fim: date | None = None,
    atualizado_desde: datetime | None = Query(
        None, description="Só o que mudou desde este momento (ISO 8601). Use para sincronizar aos poucos."
    ),
    plataforma: str | None = None,
    loja: uuid.UUID | None = None,
    pagina: int = Query(1, ge=1),
    por_pagina: int = Query(100, ge=1, le=500),
    db: Session = Depends(get_db),
):
    """Uma linha por devolução, com itens, condição, NF e custos detalhados, ordenadas pela última atualização.

    Sem `inicio`/`fim`/`atualizado_desde`, traz os últimos 30 dias. Sem dados pessoais do comprador."""
    conds = []
    if inicio or fim or not atualizado_desde:
        start, end = _period(inicio, fim)
        conds += date_range(Return.requested_at, start, end)
    if atualizado_desde:
        conds.append(Return.updated_at >= atualizado_desde)
    if p := _platform(plataforma):
        conds.append(Return.platform == p)
    if loja:
        conds.append(Return.store_id == loja)

    total = db.scalar(select(func.count()).select_from(Return).where(*conds)) or 0
    rows = db.scalars(
        select(Return)
        .where(*conds)
        # A resposta não usa os dados brutos da plataforma, que são mais da metade do que viria do banco
        .options(defer(Return.raw), selectinload(Return.items).defer(ReturnItem.raw), selectinload(Return.store))
        .order_by(Return.updated_at, Return.id)
        .offset((pagina - 1) * por_pagina)
        .limit(por_pagina)
    ).all()
    return {
        "pagina": pagina,
        "por_pagina": por_pagina,
        "total": total,
        "tem_mais": pagina * por_pagina < total,
        "devolucoes": [_return_row(r) for r in rows],
    }
