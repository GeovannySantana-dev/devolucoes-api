import json
import logging

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.concurrency import run_in_threadpool

from ..config import get_settings
from ..platforms.tiktok.client import verify_webhook_signature
from ..services.webhooks import (
    process_event,
    process_tiktok_event,
    store_mercadolivre_event,
    store_tiktok_event,
)

log = logging.getLogger(__name__)

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])


def _tiktok_signature_ok(raw: bytes, signature: str) -> bool:
    s = get_settings()
    if verify_webhook_signature(s.tiktok_app_key, s.tiktok_app_secret, raw, signature):
        return True
    # Sem o segundo app configurado não há o que conferir: segredo vazio aceitaria assinatura feita por qualquer um
    return bool(s.tiktok2_app_secret) and verify_webhook_signature(s.tiktok2_app_key, s.tiktok2_app_secret, raw, signature)


@router.post("/tiktok")
async def tiktok_webhook(request: Request, background: BackgroundTasks):
    """URL a cadastrar no Partner Center > Webhooks (evento Return status change).

    Responde rápido e processa em segundo plano, como o TikTok exige.
    """
    raw = await request.body()
    s = get_settings()
    if not _tiktok_signature_ok(raw, request.headers.get("Authorization", "")):
        log.warning("Webhook TikTok com assinatura inválida")
        raise HTTPException(401, "assinatura inválida")

    try:
        event_id = await run_in_threadpool(store_tiktok_event, raw)
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "payload inválido") from exc

    if event_id:
        if s.webhook_process_inline:
            # Cloud Run congela a CPU após a resposta: processa antes de responder
            await run_in_threadpool(process_tiktok_event, event_id)
        else:
            background.add_task(process_tiktok_event, event_id)
    return {"code": 0, "message": "success"}


@router.post("/mercadolivre")
async def mercadolivre_webhook(request: Request, background: BackgroundTasks):
    """URL de notificações do app no Mercado Livre (tópico de reclamações).

    O Mercado Livre exige resposta 200 em até 500 ms e não assina as notificações: a API só grava o
    aviso, confere o application_id e busca os dados na própria API do Mercado Livre. Se o
    processamento em segundo plano não terminar, POST /jobs/run processa os avisos pendentes.
    """
    try:
        payload = json.loads(await request.body())
    except json.JSONDecodeError as exc:
        raise HTTPException(400, "payload inválido") from exc
    event_id = await run_in_threadpool(store_mercadolivre_event, payload)
    if event_id:
        background.add_task(process_event, event_id)
    return {"status": "ok"}
