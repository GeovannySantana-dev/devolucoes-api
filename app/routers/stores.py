# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
import uuid
from dataclasses import asdict

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..db import get_db
from ..models import Store
from ..schemas import StoreCreate, StoreOut, StoreUpdate, SyncResultOut
from ..security import require_api_key
from ..services.sync import sync_all, sync_store

router = APIRouter(tags=["Lojas e sincronização"], dependencies=[Depends(require_api_key)])


def _get_store(db: Session, store_id: uuid.UUID) -> Store:
    store = db.get(Store, store_id)
    if not store:
        raise HTTPException(404, "Loja não encontrada")
    return store


@router.get("/stores", response_model=list[StoreOut])
def list_stores(db: Session = Depends(get_db)):
    return db.scalars(select(Store).order_by(Store.platform, Store.name)).all()


@router.post("/stores", response_model=StoreOut, status_code=201)
def create_store(body: StoreCreate, db: Session = Depends(get_db)):
    """Cadastro manual (Shopee, Mercado Livre, Shein). Lojas TikTok são criadas pelo fluxo OAuth."""
    store = Store(
        platform=body.platform,
        name=body.name,
        external_shop_id=body.external_shop_id,
        erp_ecommerce_id=body.erp_ecommerce_id,
        active=True,
    )
    db.add(store)
    db.commit()
    return store


@router.patch("/stores/{store_id}", response_model=StoreOut)
def update_store(store_id: uuid.UUID, body: StoreUpdate, db: Session = Depends(get_db)):
    store = _get_store(db, store_id)
    for k, v in body.model_dump(exclude_unset=True).items():
        setattr(store, k, v)
    db.commit()
    return store


@router.post("/stores/{store_id}/sync", response_model=SyncResultOut)
def sync_one(store_id: uuid.UUID, full: bool = False, db: Session = Depends(get_db)):
    """Sincroniza uma loja. `full=true` ignora a última sync e busca toda a janela configurada."""
    store = _get_store(db, store_id)
    if full:
        store.last_synced_at = None
    return asdict(sync_store(db, store))


@router.post("/sync", response_model=list[SyncResultOut])
def sync_everything(db: Session = Depends(get_db)):
    return [asdict(r) for r in sync_all(db)]
