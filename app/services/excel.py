# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Exportação dos relatórios para Excel (.xlsx), com os mesmos filtros da tela de Relatórios."""

from datetime import date, datetime, timedelta
from decimal import Decimal
from io import BytesIO
from typing import Any
from zoneinfo import ZoneInfo

from openpyxl import Workbook
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter
from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from ..config import get_settings
from ..models import Return, Store
from ..routers import reports
from ..routers.filters import date_range
from .erp import buyer_comment
from ..ui import labels

MONEY = 'R$ #,##0.00'
PCT = "0%"
DATETIME = "dd/mm/yyyy hh:mm"
DATE = "dd/mm/yyyy"
HEADER_FILL = PatternFill("solid", fgColor="0F6E56")
HEADER_FONT = Font(bold=True, color="FFFFFF")

LIABILITY = {"seller": "Loja", "platform": "Marketplace", "buyer": "Comprador", "none": "—"}
DECISION = {"approved": "Aceita", "rejected": "Recusada"}


def _local(value: datetime | None, tz: ZoneInfo) -> datetime | None:
    # Excel não guarda fuso: grava no horário de São Paulo, sem o fuso
    return value.astimezone(tz).replace(tzinfo=None) if value else None


def _num(value: Any) -> float | None:
    return float(value) if isinstance(value, Decimal | int | float) else value


def _sheet(wb: Workbook, title: str, headers: list[str], rows: list[list[Any]], formats: dict[int, str] | None = None,
           widths: dict[int, int] | None = None, first: bool = False):
    ws = wb.active if first else wb.create_sheet()
    ws.title = title
    ws.append(headers)
    for cell in ws[1]:
        cell.fill, cell.font = HEADER_FILL, HEADER_FONT
        cell.alignment = Alignment(vertical="center", wrap_text=True)
    for row in rows:
        ws.append([_num(v) for v in row])
    for col, fmt in (formats or {}).items():
        for (cell,) in ws.iter_rows(min_row=2, min_col=col, max_col=col):
            cell.number_format = fmt
    for i, header in enumerate(headers, start=1):
        ws.column_dimensions[get_column_letter(i)].width = (widths or {}).get(i, max(12, min(40, len(header) + 4)))
    ws.freeze_panes = "A2"
    if rows:
        ws.auto_filter.ref = ws.dimensions
    return ws


def _store_name(db: Session, store_id) -> str:
    if not store_id:
        return ""
    store = db.get(Store, store_id)
    return f"loja: {store.label}" if store else ""


def build_report_xlsx(
    db: Session,
    start: date,
    end: date,
    platform: str | None,
    period_label: str,
    store_id=None,
    extra: list | None = None,
    filters_note: str = "",
) -> bytes:
    """`store_id`, `extra` (condições de motivo/SKU do painel) e `filters_note` repetem os filtros do painel."""
    tz = ZoneInfo(get_settings().timezone)
    extra = list(extra or [])
    f = reports.Filters(date_from=start, date_to=end, platform=platform, store_id=store_id)
    f.conds += extra
    summary = reports.summary(f=f, db=db)
    costs = reports.costs(f=f, group_by="reason", db=db)
    store_costs = {g["key"]: g for g in reports.costs(f=f, group_by="store", db=db)["groups"]}
    wb = Workbook()

    # ---------- Resumo ----------
    t = costs["totals"]
    _sheet(
        wb, "Resumo", ["Indicador", "Valor"],
        [
            ["Período", period_label],
            ["Filtros", " · ".join(x for x in (labels.PLATFORM.get(platform or "", "Todos os marketplaces"),
                                               _store_name(db, store_id), filters_note) if x)],
            ["Gerado em", datetime.now(tz).strftime("%d/%m/%Y %H:%M")],
            ["Devoluções", summary["total"]],
            ["Concluídas", summary["completed"]],
            ["Recusadas", summary["rejected"]],
            ["Canceladas", summary["cancelled"]],
            ["Reembolsado aos clientes (R$)", summary["refunded_amount"]],
            ["Custo total para a empresa (R$)", t["total"]],
            ["  Frete de devolução pago pela loja (R$)", t["shipping"]],
            ["  Peças perdidas (defeito/item diferente) (R$)", t["product_loss"]],
            ["  Custo operacional (R$)", t["operational"]],
            ["  Frete de ida e taxas da venda perdidos (TikTok) (R$)", t.get("lost_sale", 0)],
            ["Devoluções com custo confirmado pelo extrato (TikTok)", t.get("settled_returns", 0)],
            ["Tempo médio até resolver (horas)", summary["avg_resolution_hours"]],
        ],
        widths={1: 46, 2: 34}, first=True,
    )
    ws = wb["Resumo"]
    for r in range(9, 14):
        ws.cell(row=r, column=2).number_format = MONEY
    ws.cell(row=8, column=2).number_format = MONEY

    # ---------- Por loja ----------
    _sheet(
        wb, "Por loja",
        ["Marketplace", "Loja", "Devoluções", "Pendentes", "Concluídas", "Recusadas", "Reembolsado (R$)", "Custo (R$)"],
        [
            [labels.PLATFORM.get(s["platform"], s["platform"]), s["store_name"], s["total"], s["needs_action"],
             s["completed"], s["rejected"], s["refunded_amount"], store_costs.get(s["store_name"], {}).get("total", 0)]
            for s in reports.by_store(f=f, db=db)
        ],
        formats={7: MONEY, 8: MONEY}, widths={2: 32},
    )

    # ---------- Produtos ----------
    sku_costs = {g["key"]: g for g in reports.costs(f=f, group_by="sku", db=db)["groups"]}
    _sheet(
        wb, "Produtos",
        ["SKU", "Produto", "Devoluções", "Unidades", "Reembolso (R$)", "Defeitos", "Taxa de defeito",
         "Item diferente", "Custo (R$)"],
        [
            [p["seller_sku"] or "SEM SKU", p["product_name"], p["returns"], p["units"], p["amount"], p["defects"],
             p["defect_rate"], p["mismatches"], sku_costs.get(p["seller_sku"], {}).get("total")]
            for p in reports.by_product(f=f, limit=100_000, db=db)
        ],
        formats={5: MONEY, 7: PCT, 9: MONEY}, widths={1: 18, 2: 60},
    )

    # ---------- Motivos ----------
    _sheet(
        wb, "Motivos",
        ["Motivo", "Devoluções", "Frete (R$)", "Peças perdidas (R$)", "Operacional (R$)", "Venda perdida (R$)",
         "Custo total (R$)", "Custo médio (R$)", "Reembolsado (R$)"],
        [
            [g["key"], g["returns"], g["shipping"], g["product_loss"], g["operational"], g.get("lost_sale", 0),
             g["total"], g["avg_per_return"], g["refunded_amount"]]
            for g in sorted(costs["groups"], key=lambda g: g["returns"], reverse=True)
        ],
        formats={c: MONEY for c in range(3, 10)}, widths={1: 50},
    )

    # ---------- Por mês (últimos 12 meses) ----------
    year_ago = (end.replace(day=1) - timedelta(days=330)).replace(day=1)
    fm = reports.Filters(date_from=year_ago, date_to=end, platform=platform, store_id=store_id)
    fm.conds += extra
    months = reports.timeseries(f=fm, granularity="month", db=db)
    _sheet(
        wb, "Por mês", ["Mês", "Devoluções", "Concluídas", "Recusadas", "Valor (R$)"],
        [[labels.month_label(m["period"]), m["total"], m["completed"], m["rejected"], m["amount"]] for m in months],
        formats={5: MONEY},
    )

    # ---------- Devoluções (uma linha por devolução) ----------
    conds = date_range(Return.requested_at, start, end) + extra
    if platform:
        conds.append(Return.platform == platform)
    if store_id:
        conds.append(Return.store_id == store_id)
    rows = []
    for r in db.scalars(
        select(Return)
        .where(*conds)
        .options(selectinload(Return.items), selectinload(Return.store))
        .order_by(Return.requested_at.desc())
        .execution_options(yield_per=500)
    ):
        skus = ", ".join(i.seller_sku or "SEM SKU" for i in r.items)
        names = " | ".join(i.product_name or "" for i in r.items)
        rows.append([
            _local(r.requested_at, tz), labels.PLATFORM.get(r.platform, r.platform), r.store.label if r.store else "",
            r.external_return_id, r.external_order_id, r.erp_order_number,
            "Não entregue" if r.kind == "undelivered" else "Devolução", labels.status_label(r.status),
            labels.RETURN_TYPE.get(r.return_type, r.return_type), r.reason_text or r.reason_code, buyer_comment(r),
            skus, names, sum(i.quantity or 0 for i in r.items), r.refund_amount,
            labels.CONDITION.get(r.item_condition or "", "") if r.item_condition else "",
            "Conferência" if r.condition_source == "manual" else ("Sugestão" if r.item_condition else ""),
            _local(r.received_at, tz), r.received_by, DECISION.get(r.decision or "", ""), r.decided_by,
            _local(r.action_deadline, tz), r.tracking_number,
            labels.LOGISTICS.get(r.logistics_status or "", ("",))[0] if r.logistics_status else "",
            r.erp_return_invoice_number, labels.INVOICE.get(r.erp_return_invoice_status, ""),
            labels.STOCK.get(r.erp_stock_status, "") if r.received_at else "",
            LIABILITY.get(r.liability or "none", ""), r.cost_return_shipping, r.cost_product_loss,
            r.cost_operational, r.cost_lost_sale, r.cost_total,
            "Confirmado (extrato)" if r.cost_settled_at else "Estimado", r.erp_last_error,
        ])
    _sheet(
        wb, "Devoluções",
        ["Solicitada em", "Marketplace", "Loja", "Nº devolução", "Pedido marketplace", "Pedido Olist", "Tipo",
         "Status", "Modalidade", "Motivo", "Comentário do comprador", "SKU(s)", "Produto(s)", "Unidades",
         "Valor (R$)", "Condição", "Origem da condição", "Conferida em", "Conferida por", "Decisão", "Decidida por",
         "Prazo", "Rastreio", "Pacote", "NF devolução", "Situação NF", "Estoque", "Frete pago por", "Frete (R$)",
         "Peça perdida (R$)", "Operacional (R$)", "Venda perdida (R$)", "Custo total (R$)", "Custo", "Erro Olist"],
        rows,
        formats={1: DATETIME, 15: MONEY, 18: DATETIME, 22: DATETIME, 29: MONEY, 30: MONEY, 31: MONEY, 32: MONEY,
                 33: MONEY},
        widths={1: 16, 3: 24, 4: 22, 5: 22, 10: 32, 11: 45, 12: 22, 13: 50, 16: 30, 19: 30, 21: 30, 34: 20, 35: 40},
    )

    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def build_transfer_xlsx(batch_number: int | str, rows: list[dict[str, Any]], returns: list[Any]) -> bytes:
    """Lista do lote de transferência SP -> MS: peças por SKU (para a NF) e as devoluções de origem."""
    wb = Workbook()
    money = "#,##0.00"
    _sheet(wb, f"Lote {batch_number}", ["SKU", "Produto", "Variação", "Quantidade", "Valor (R$)", "Pedidos"],
           [[r["sku"], r["name"], r["variant"], r["quantity"], r["amount"], ", ".join(r["orders"])] for r in rows],
           formats={5: money}, widths={1: 22, 2: 50, 3: 18, 6: 40}, first=True)
    _sheet(wb, "Devoluções", ["Marketplace", "Pedido", "NF de devolução", "Conferida em"],
           [[r.platform, r.external_order_id, r.erp_return_invoice_number,
             r.received_at.strftime("%d/%m/%Y %H:%M") if r.received_at else None] for r in returns],
           widths={2: 24})
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def build_productivity_xlsx(rows: list[dict[str, Any]]) -> bytes:
    """Produtividade por pessoa e por dia (mesmos números da API do sistema de eficiência)."""
    wb = Workbook()
    _sheet(wb, "Produtividade",
           ["Dia", "Pessoa", "E-mail", "Pacotes", "Em turno (min)", "Pausas (min)", "Ativo (min)", "Aproveitamento",
            "Pacotes/hora", "Tempo/pacote (s, mediana)", "Custo/hora (R$)", "Custo/pacote (R$)"],
           [[r["date"], r["name"], r["email"], r["inspections"], r["shift_minutes"], r["pause_minutes"],
             r["active_minutes"], r["utilization"], r["packages_per_hour"], r["median_seconds_per_package"],
             r["hourly_cost"], r["cost_per_package"]] for r in rows],
           formats={8: "0%", 11: "#,##0.00", 12: "#,##0.00"}, widths={2: 28, 3: 30}, first=True)
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def build_losses_xlsx(month_label: str, rows: list[dict[str, Any]], skus: list[dict[str, Any]]) -> bytes:
    """Perdas do mês (defeito, descarte): base da NF de baixa por perda (CFOP 5.927) e prova de cada peça."""
    wb = Workbook()
    tz = ZoneInfo(get_settings().timezone)
    base = (get_settings().app_url or get_settings().public_base_url).rstrip("/")
    _sheet(wb, "Por SKU (NF 5.927)", ["SKU", "Produto", "Variação", "Quantidade", "Pedidos"],
           [[s["sku"], s["name"], s["variant"], s["quantity"], ", ".join(s["orders"])] for s in skus],
           widths={1: 22, 2: 50, 3: 18, 5: 40}, first=True)
    lines = []
    for r in rows:
        ret = r["ret"]
        for n, i in enumerate(r["items"]):
            lines.append([
                ret.received_at.astimezone(tz).replace(tzinfo=None) if ret.received_at else None,
                labels.PLATFORM.get(ret.platform, ret.platform), ret.external_order_id, i["sku"], i["name"],
                i["quantity"], labels.DEFECT_REASON.get(r["reason"] or "", "Sem motivo"), r["notes"],
                r["photos"], ret.erp_return_invoice_number, ret.received_by, r["cost"] if n == 0 else None,
                f"{base}/app/devolucoes/{ret.id}",
            ])
    _sheet(wb, f"Peças {month_label}",
           ["Conferida em", "Marketplace", "Pedido", "SKU", "Produto", "Quantidade", "Motivo", "Detalhe",
            "Fotos", "NF de devolução", "Conferida por", "Custo estimado", "Fotos e histórico"],
           lines, formats={1: DATETIME, 12: MONEY}, widths={3: 24, 4: 22, 5: 40, 8: 36, 11: 28, 13: 60})
    out = BytesIO()
    wb.save(out)
    return out.getvalue()


def build_costs_xlsx(models: list[dict[str, Any]], pieces_by_model: dict[str, int], missing: list[dict[str, Any]]) -> bytes:
    """Custos por modelo (o que está valendo no app) e os SKUs devolvidos sem modelo."""
    wb = Workbook()
    _sheet(wb, "Modelos", ["Código", "Modelo", "Começos de SKU", "Custo da peça", "Peças devolvidas"],
           [[m["code"], m["name"], ", ".join(m["prefixes"]), m["cost"], pieces_by_model.get(m["code"], 0)] for m in models],
           formats={4: MONEY}, widths={2: 40, 3: 40}, first=True)
    _sheet(wb, "SKUs sem modelo", ["SKU", "Produto", "Peças devolvidas"],
           [[s["sku"], s["name"], s["pieces"]] for s in missing], widths={1: 24, 2: 50})
    out = BytesIO()
    wb.save(out)
    return out.getvalue()
