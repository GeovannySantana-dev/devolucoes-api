import uuid

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..erp.olist import OlistError
from ..models import ReturnReasonRule, Store
from ..schemas import ConditionIn, ReasonRuleIn, ReceiveIn, ReturnDetailOut, UndeliveredIn
from ..security import require_api_key
from ..services import erp, inspection
from .returns import _load

router = APIRouter(tags=["Conferência e Olist ERP"], dependencies=[Depends(require_api_key)])


def _run(fn):
    try:
        return fn()
    except erp.ErpError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    except OlistError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/returns/{return_id}/receive", response_model=ReturnDetailOut)
def receive(return_id: uuid.UUID, body: ReceiveIn, db: Session = Depends(get_db)):
    """Conferência do pacote devolvido: define se o item volta ao estoque ou é defeito.

    Em seguida (se `create_invoice`) gera a NF de devolução no Olist. Erros do Olist ficam em
    `erp_last_error` e no histórico, sem desfazer o recebimento.
    """
    ret = _load(db, return_id)
    try:
        inspection.receive_return(db, ret, body.actor, body.condition, body.notes, body.create_invoice)
    except inspection.InspectionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return _load(db, return_id)


@router.post("/returns/{return_id}/condition", response_model=ReturnDetailOut)
def set_condition(return_id: uuid.UUID, body: ConditionIn, db: Session = Depends(get_db)):
    """Corrige a condição do item (antes de o estoque ser lançado)."""
    ret = _load(db, return_id)
    try:
        inspection.set_condition(db, ret, body.actor, body.condition)
    except inspection.InspectionError as exc:
        raise HTTPException(exc.status_code, str(exc)) from exc
    return _load(db, return_id)


@router.post("/returns/{return_id}/erp/link", response_model=ReturnDetailOut)
def erp_link(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Busca o pedido e a NF de venda no Olist."""
    ret = _load(db, return_id)
    _run(lambda: erp.link_return(db, ret))
    return _load(db, return_id)


@router.post("/returns/{return_id}/erp/invoice", response_model=ReturnDetailOut)
def erp_invoice(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Gera (ou regera, se a anterior foi cancelada) a NF de devolução no Olist."""
    ret = _load(db, return_id)
    _run(lambda: erp.create_return_invoice(db, ret))
    return _load(db, return_id)


@router.post("/returns/{return_id}/erp/emit", response_model=ReturnDetailOut)
def erp_emit(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Transmite a NF de devolução para a SEFAZ pelo Olist."""
    ret = _load(db, return_id)
    _run(lambda: erp.emit_return_invoice(db, ret))
    return _load(db, return_id)


@router.post("/returns/{return_id}/erp/refresh", response_model=ReturnDetailOut)
def erp_refresh(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Consulta a situação da NF no Olist; se autorizada, processa o estoque."""
    ret = _load(db, return_id)
    _run(lambda: erp.refresh_invoice_status(db, ret))
    return _load(db, return_id)


@router.post("/returns/{return_id}/erp/stock", response_model=ReturnDetailOut)
def erp_stock(return_id: uuid.UUID, db: Session = Depends(get_db)):
    """Processa o estoque manualmente (revenda: lança; defeito: não lança)."""
    ret = _load(db, return_id)
    _run(lambda: erp.post_stock(db, ret))
    return _load(db, return_id)


# ---------- retorno de mercadoria não entregue ----------

@router.post("/returns/undelivered", response_model=ReturnDetailOut, status_code=201)
def register_undelivered(body: UndeliveredIn, db: Session = Depends(get_db)):
    """Registra manualmente um pacote que voltou pela transportadora sem ser entregue.

    Normalmente não é preciso: a API importa sozinha os pedidos marcados como 'Não entregue' no Olist.
    """
    store = db.get(Store, body.store_id)
    if not store:
        raise HTTPException(404, "Loja não encontrada")
    ret = _run(lambda: erp.register_undelivered(db, store, body.order_number, body.actor, body.notes))
    return _load(db, ret.id)


@router.post("/erp/import-undelivered")
def import_undelivered(db: Session = Depends(get_db)):
    """Importa agora os pedidos 'Não entregue' do Olist (também roda automaticamente).

    `unmapped` lista integrações do Olist sem loja vinculada: use PATCH /stores/{id} com `erp_ecommerce_id`.
    """
    return _run(lambda: erp.import_undelivered(db))


# ---------- regras de classificação ----------

@router.get("/return-reason-rules", response_model=list[ReasonRuleIn], tags=["Motivos de recusa"])
def list_rules(db: Session = Depends(get_db)):
    return db.scalars(select(ReturnReasonRule).order_by(ReturnReasonRule.priority, ReturnReasonRule.keyword)).all()


@router.put("/return-reason-rules/{keyword}", response_model=ReasonRuleIn, tags=["Motivos de recusa"])
def upsert_rule(keyword: str, body: ReasonRuleIn, db: Session = Depends(get_db)):
    if keyword != body.keyword:
        raise HTTPException(422, "keyword da URL difere do body")
    rule = db.get(ReturnReasonRule, keyword) or ReturnReasonRule(keyword=keyword)
    rule.condition = body.condition
    rule.priority = body.priority
    db.add(rule)
    db.commit()
    return rule


@router.delete("/return-reason-rules/{keyword}", status_code=204, tags=["Motivos de recusa"])
def delete_rule(keyword: str, db: Session = Depends(get_db)):
    rule = db.get(ReturnReasonRule, keyword)
    if not rule:
        raise HTTPException(404, "Regra não encontrada")
    db.delete(rule)
    db.commit()
