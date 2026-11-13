# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Custo de cada peça pelo modelo, a partir do início do SKU (tabela product_cost_models).

Vale o início mais longo que casar, sempre num limite de "-" (VCA-PRETLIS-M → VCA-PRETLIS, listrado; VCA-MANTEI-G →
VCA, liso). Início só com números (SKUs antigos do Mercado Livre, ex.: 300000015-CRU-M) casa direto pelo começo.
SKU sem modelo usa o custo padrão (COST_PRODUCT_DEFAULT).

A tabela é pequena e muda pouco: fica em memória e é relida a cada 10 minutos.
"""

import logging
import re
import time
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..config import get_settings
from ..models import ProductCostModel

log = logging.getLogger(__name__)
REFRESH_SECONDS = 600

_cache: dict[str, tuple[str, str, Decimal]] | None = None
_loaded_at = 0.0


def _load() -> dict[str, tuple[str, str, Decimal]]:
    from ..db import new_session

    db = new_session()
    try:
        rows = db.scalars(select(ProductCostModel)).all()
        return {r.sku_prefix.upper(): (r.model_code, r.model_name, Decimal(r.unit_cost)) for r in rows}
    finally:
        db.close()


def table() -> dict[str, tuple[str, str, Decimal]]:
    global _cache, _loaded_at
    if _cache is None or time.monotonic() - _loaded_at > REFRESH_SECONDS:
        try:
            _cache = _load()
        except Exception:  # sem banco (testes, falha de rede): segue com o que tinha ou só o custo padrão
            log.exception("não foi possível ler os custos por modelo")
            _cache = _cache or {}
        _loaded_at = time.monotonic()
    return _cache


def set_table(data: dict[str, tuple[str, str, Decimal]] | None) -> None:
    """Troca a tabela em memória (testes) ou força reler do banco na próxima consulta (None)."""
    global _cache, _loaded_at
    _cache, _loaded_at = (None, 0.0) if data is None else ({k.upper(): v for k, v in data.items()}, time.monotonic())


def _matches(sku: str, prefix: str) -> bool:
    return sku == prefix or sku.startswith(prefix if prefix.isdigit() else prefix + "-")


def model_for(sku: str | None) -> tuple[str, str, Decimal] | None:
    """(código, modelo, custo) do modelo da peça, ou None quando o SKU não tem modelo cadastrado."""
    sku = (sku or "").strip().upper()
    if not sku:
        return None
    hits = [p for p in table() if _matches(sku, p)]
    return table()[max(hits, key=len)] if hits else None


def unit_cost(sku: str | None) -> Decimal:
    found = model_for(sku)
    return found[2] if found else Decimal(str(get_settings().cost_product_default))


# ---------- tela "Custos por modelo" (gestor) ----------

class CostError(Exception):
    pass


PREFIX_RE = re.compile(r"^[A-Z0-9][A-Z0-9-]{0,39}$")


def parse_cost(text: str) -> Decimal:
    """'31,50' / 'R$ 31,50' / '31.50' -> Decimal('31.50')."""
    raw = (text or "").replace("R$", "").strip()
    if "," in raw:
        raw = raw.replace(".", "").replace(",", ".")
    try:
        value = Decimal(raw)
    except ArithmeticError:
        raise CostError("Custo inválido: use só números, ex.: 31,50") from None
    if value < 0 or value > 10000:
        raise CostError("Custo fora do esperado (entre R$ 0 e R$ 10.000)")
    return value.quantize(Decimal("0.01"))


def _clean_prefix(prefix: str) -> str:
    p = (prefix or "").strip().upper().rstrip("-")
    if not PREFIX_RE.match(p):
        raise CostError(f"Começo de SKU inválido: {prefix!r} (letras, números e traço, ex.: VCA-PRETLIS)")
    return p


def _changed() -> None:
    set_table(None)  # relê do banco na próxima consulta (outros processos releem em até 10 min)


def models(db: Session) -> list[dict]:
    """Um item por modelo (código), com os começos de SKU e o custo."""
    out: dict[str, dict] = {}
    for r in db.scalars(select(ProductCostModel).order_by(ProductCostModel.model_code, ProductCostModel.sku_prefix)):
        m = out.setdefault(r.model_code, {"code": r.model_code, "name": r.model_name, "cost": Decimal(r.unit_cost),
                                          "prefixes": [], "updated_at": r.updated_at})
        m["prefixes"].append(r.sku_prefix)
        if r.updated_at and (m["updated_at"] is None or r.updated_at > m["updated_at"]):
            m["updated_at"] = r.updated_at
    return list(out.values())


def set_model_cost(db: Session, code: str, text: str) -> Decimal:
    cost = parse_cost(text)
    rows = db.scalars(select(ProductCostModel).where(ProductCostModel.model_code == code)).all()
    if not rows:
        raise CostError(f"Modelo {code} não encontrado")
    for r in rows:
        r.unit_cost = cost
        r.updated_at = func.now()
    db.commit()
    _changed()
    return cost


def add_prefix(db: Session, code: str, prefix: str) -> str:
    prefix = _clean_prefix(prefix)
    model = db.scalar(select(ProductCostModel).where(ProductCostModel.model_code == code).limit(1))
    if not model:
        raise CostError(f"Modelo {code} não encontrado")
    taken = db.get(ProductCostModel, prefix)
    if taken:
        raise CostError(f"O começo {prefix} já é do modelo {taken.model_code}")
    db.add(ProductCostModel(sku_prefix=prefix, model_code=code, model_name=model.model_name, unit_cost=model.unit_cost))
    db.commit()
    _changed()
    return prefix


def remove_prefix(db: Session, prefix: str) -> None:
    row = db.get(ProductCostModel, (prefix or "").upper())
    if not row:
        raise CostError("Começo de SKU não encontrado")
    others = db.scalar(select(func.count()).where(ProductCostModel.model_code == row.model_code))
    if others <= 1:
        raise CostError(f"{row.model_code} precisa de pelo menos um começo de SKU")
    db.delete(row)
    db.commit()
    _changed()


def create_model(db: Session, code: str, name: str, prefixes: str, cost_text: str) -> str:
    code = (code or "").strip().upper()
    name = (name or "").strip()
    if not code or not name:
        raise CostError("Preencha o código e o nome do modelo")
    if db.scalar(select(ProductCostModel).where(ProductCostModel.model_code == code).limit(1)):
        raise CostError(f"Já existe o modelo {code}")
    cost = parse_cost(cost_text)
    clean = [_clean_prefix(p) for p in (prefixes or "").split(",") if p.strip()]
    if not clean:
        raise CostError("Informe pelo menos um começo de SKU (ex.: VL)")
    for p in clean:
        taken = db.get(ProductCostModel, p)
        if taken:
            raise CostError(f"O começo {p} já é do modelo {taken.model_code}")
        db.add(ProductCostModel(sku_prefix=p, model_code=code, model_name=name[:120], unit_cost=cost))
    db.commit()
    _changed()
    return code


def returned_skus(db: Session) -> list[dict]:
    """SKUs que já voltaram em devoluções: peças e nome do produto (para cobertura e "sem modelo")."""
    from ..models import ReturnItem

    rows = db.execute(
        select(
            func.upper(func.trim(ReturnItem.seller_sku)),
            func.max(ReturnItem.product_name),
            func.sum(func.coalesce(ReturnItem.quantity, 1)),
        )
        .where(ReturnItem.seller_sku.is_not(None), func.trim(ReturnItem.seller_sku) != "")
        .group_by(func.upper(func.trim(ReturnItem.seller_sku)))
    ).all()
    return [{"sku": sku, "name": name, "pieces": int(pieces or 0)} for sku, name, pieces in rows]


def coverage(db: Session) -> dict:
    """Peças devolvidas por modelo, cobertura e os SKUs sem modelo (usam o custo padrão)."""
    pieces_by_model: dict[str, int] = {}
    missing: list[dict] = []
    total = covered = 0
    for s in returned_skus(db):
        total += s["pieces"]
        found = model_for(s["sku"])
        if found:
            covered += s["pieces"]
            pieces_by_model[found[0]] = pieces_by_model.get(found[0], 0) + s["pieces"]
        else:
            missing.append(s)
    missing.sort(key=lambda s: -s["pieces"])
    return {"total": total, "covered": covered, "by_model": pieces_by_model, "missing": missing}
