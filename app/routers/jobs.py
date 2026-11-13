# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Rotinas periódicas disparadas por um agendador externo (Cloud Scheduler).

No Cloud Run a instância "dorme" sem tráfego, então o agendador interno (APScheduler)
fica desligado (ENABLE_SCHEDULER=false) e o Cloud Scheduler chama POST /jobs/run.
"""

import logging
from dataclasses import asdict
from datetime import UTC, datetime

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from ..config import get_settings
from ..db import get_db
from ..security import require_api_key
from ..services import erp
from ..services.alerts import run_alerts
from ..services.backup import run_daily_backup
from ..services.camera import process_pending_clips
from ..services.health import check_health, check_public_ip
from ..services.media_backup import run_backup as backup_media
from ..services.ml_review import review_ml_returns
from ..services.post_inspection import confirm_inspected
from ..services.shein_receipt import confirm_shein_receipts
from ..services.sync import sync_all
from ..services.settlement import settle_all_costs
from ..services.tokens import refresh_expiring_tokens
from ..services.tracking import refresh_shopee_tracking
from ..services.video_compress import compress_pending
from ..services.webhooks import process_pending_events

log = logging.getLogger(__name__)

router = APIRouter(prefix="/jobs", tags=["Infra"], dependencies=[Depends(require_api_key)])


def _step(results: dict, name: str, fn) -> None:
    started = datetime.now(UTC)
    try:
        out = fn()
        results[name] = {"ok": True, "result": out}
    except Exception as exc:  # uma rotina com erro não impede as demais
        log.exception("Rotina %s falhou", name)
        results[name] = {"ok": False, "error": str(exc)}
    results[name]["seconds"] = round((datetime.now(UTC) - started).total_seconds(), 1)


@router.post("/run")
def run_jobs(db: Session = Depends(get_db)):
    s = get_settings()
    results: dict = {}
    _step(results, "refresh_tokens", lambda: refresh_expiring_tokens(db))
    _step(results, "webhooks_pending", process_pending_events)
    _step(results, "sync", lambda: [asdict(r) for r in sync_all(db)])
    _step(results, "shopee_tracking", lambda: refresh_shopee_tracking(db))
    _step(results, "aprovar_conferidas", lambda: confirm_inspected(db))  # TikTok e Shopee: revenda conferida
    _step(results, "ml_revisao", lambda: review_ml_returns(db))  # "Já revisei" no ML das conferidas como revenda
    _step(results, "shein_recebimento", lambda: confirm_shein_receipts(db))  # confirma na Shein o que foi conferido
    _step(results, "extrato", lambda: settle_all_costs(db))  # custo confirmado das já fechadas (financeiro)
    _step(results, "camera", lambda: process_pending_clips(db))  # vídeo da bancada das conferências
    # Avisos por e-mail depois da sincronização, para usar prazos e rastreio atualizados
    _step(results, "alerts", lambda: run_alerts(db))
    _step(results, "backup", run_daily_backup)  # uma vez por dia
    _step(results, "videos", lambda: compress_pending(db))  # vídeo do celular: ~70 MB -> ~7 MB
    _step(results, "copia_midias", lambda: backup_media(db))  # fotos (e vídeos, no plano pago) no Supabase
    if s.olist_api_token:
        _step(results, "olist_link", lambda: erp.link_pending(db))
        _step(results, "olist_rascunhos", lambda: erp.create_pending_invoices(db))  # conferidas sem NF
        _step(results, "olist_autorizar", lambda: erp.emit_checked_drafts(db))  # confere e autoriza
        _step(results, "olist_invoices", lambda: erp.check_invoices(db))
        if s.olist_import_undelivered:
            _step(results, "olist_undelivered", lambda: erp.import_undelivered(db))
    _step(results, "ip", lambda: check_public_ip(db))  # IP de internet mudou? (lista branca da Shein)
    # Por último: avisa o gestor se algo acima falhou ou se alguma loja parou de sincronizar
    snapshot = dict(results)
    _step(results, "health", lambda: check_health(db, snapshot))
    return results
