# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    # env_ignore_empty: variáveis vazias no .env (ex.: OLIST_DEFECT_DEPOSIT_ID=) usam o padrão
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", env_ignore_empty=True)

    database_url: str
    # O Session pooler do Supabase aceita 15 conexões no total (telas + rotinas): cada processo usa
    # no máximo pool + overflow; acima disso a requisição espera em vez de dar 500
    db_pool_size: int = 4
    db_max_overflow: int = 2
    api_key: str
    # Chave Fernet que cifra os tokens das lojas no banco (app/crypto.py). Guarde com cuidado.
    token_encryption_key: str = ""
    public_base_url: str = "http://localhost:8000"

    tiktok_app_key: str = ""
    tiktok_app_secret: str = ""
    tiktok_service_id: str = ""
    # Segundo app privado do TikTok, para a loja de outro parceiro (vazio = não usado)
    tiktok2_app_key: str = ""
    tiktok2_app_secret: str = ""
    tiktok2_service_id: str = ""
    tiktok_api_base: str = "https://open-api.tiktokglobalshop.com"
    tiktok_auth_base: str = "https://auth.tiktok-shops.com"
    tiktok_authorize_url: str = "https://services.tiktokshop.com/open/authorize"
    tiktok_locale: str = "pt-BR"

    # Shopee Open Platform (Console > App List > Live Partner_id / Partner Key)
    shopee_partner_id: int | None = None
    shopee_partner_key: str = ""
    shopee_api_base: str = "https://partner.shopeemobile.com"
    # E-mail informado à Shopee ao contestar (recusar) uma devolução
    shopee_dispute_email: str = ""

    # Mercado Livre Developers > Suas aplicações (Client ID / Client Secret)
    # Câmera da bancada (gravação contínua no servidor + recorte por conferência). Vazio = desligada.
    camera_rtsp_url: str = ""  # ex.: rtsp://usuario:senha@192.168.0.50:554/stream1 (fica só no .env)
    camera_dir: str = "gravacoes"
    camera_retention_days: int = 30
    camera_segment_seconds: int = 60
    camera_clip_pad_seconds: int = 30
    camera_max_clip_minutes: int = 30
    ffmpeg_path: str = "ffmpeg"  # sem ffmpeg instalado, usa o que vem no pacote imageio-ffmpeg
    # Vídeos do celular chegam com ~70 MB: o servidor recomprime (lado menor até VIDEO_MAX_SIDE px)
    video_compress: bool = True
    video_max_side: int = 720
    video_crf: int = 28  # qualidade do H.264: maior = arquivo menor
    # Cópia das fotos (e vídeos) no Supabase Storage: Project Settings > API Keys > chave secreta (service_role)
    supabase_url: str = ""
    supabase_service_key: str = ""
    media_backup_bucket: str = "midias"
    media_backup_videos: bool = False  # liga quando o plano pago (100 GB) estiver ativo
    media_backup_max_gb: float = 0.9  # plano grátis: 1 GB no total

    shein_app_id: str = ""
    shein_app_secret: str = ""
    shein_api_base: str = "https://openapi.sheincorp.com"  # Autooperação (produção)
    shein_auth_url: str = "https://openapi-sem.sheincorp.com/#/empower"

    mercadolivre_client_id: str = ""
    mercadolivre_client_secret: str = ""
    # Precisa ser idêntica à URI de redirect cadastrada no app
    mercadolivre_redirect_uri: str = ""
    mercadolivre_api_base: str = "https://api.mercadolibre.com"
    mercadolivre_auth_url: str = "https://auth.mercadolivre.com.br/authorization"

    # Olist ERP (API v2): Configurações > Token API
    olist_api_token: str = ""
    olist_api_base: str = "https://api.tiny.com.br/api2"
    # off   = só vincula pedido/nota e registra a condição; a NF é feita à mão no Olist
    # draft = a API cria a NF de devolução no Olist e a equipe confere e emite lá
    # emit  = a API cria e já transmite a NF para a SEFAZ
    olist_invoice_mode: str = "draft"
    # Com modo draft: a rotina confere cada rascunho do app contra a NF de venda e, se tudo bater, autoriza na Sefaz
    olist_auto_emit: bool = False
    # Venda apagada do Olist: .zip com os XMLs das NF-e de saída (separados por ;) para montar a devolução
    olist_sale_xml_zips: str = ""
    olist_natureza_operacao: str = "Devolução de venda"
    olist_natureza_resale_id: int | None = None  # natureza para itens que voltam ao estoque
    olist_natureza_defect_id: int | None = None  # natureza para itens com defeito
    # Natureza de devolução pelo CFOP da NF de venda: "202:id,201:id,411:id,202S:id" (S = venda feita no
    # Simples, sem destaque de impostos). Preenchida, tem prioridade sobre as três opções acima.
    olist_return_naturezas: str = ""
    olist_invoice_finalidade: int = 4  # 4 = devolução de mercadoria (NF-e finNFe)
    # Valor do item na NF de devolução:
    # marketplace  = valor real da peça no marketplace (recomendado: o Olist rateia o desconto do pedido
    #                proporcionalmente ao preço cheio, o que difere do desconto aplicado por item no TikTok)
    # sale_invoice = rateio proporcional do desconto da NF de venda
    olist_return_value_source: str = "marketplace"
    olist_frete_por_conta: str = "R"
    # A API lança o estoque da NF autorizada apenas para itens revendáveis.
    # Use false se a natureza de operação do Olist já lança estoque sozinha.
    olist_post_stock: bool = True
    olist_resale_deposit_id: int | None = None  # vazio = depósito padrão
    olist_defect_deposit_id: int | None = None  # vazio = defeito não entra em estoque nenhum
    # O "lançar estoque" da nota no Olist ignora o depósito pedido e lança sempre no padrão. Com os nomes abaixo, o app
    # transfere em seguida cada peça do padrão para o depósito certo (a API de estoque só aceita depósito pelo nome).
    olist_default_deposit_name: str = ""
    olist_resale_deposit_name: str = ""
    olist_defect_deposit_name: str = ""
    olist_link_batch_size: int = 20

    # Filial (MS, conta 2 do Olist): recebe as peças de revenda por transferência da matriz (SP)
    olist_filial_api_token: str = ""
    olist_filial_deposit: str = "Geral"  # depósito da conta 2 onde entram as peças transferidas

    # Retorno de mercadoria não entregue ao destinatário
    olist_natureza_undelivered: str = "Retorno de mercadoria nao entregue ao destinatario"
    olist_natureza_undelivered_id: int | None = None
    olist_undelivered_finalidade: int = 4
    olist_undelivered_situacao: str = "nao_entregue"  # filtro da situação "Não entregue" na API v2
    olist_undelivered_lookback_days: int = 15
    olist_import_undelivered: bool = True

    # Custos da devolução (financeiro)
    cost_product_default: float = 40.0  # custo padrão da peça, perdido quando há defeito
    cost_operational_per_return: float = 4.0  # receber, conferir, reembalar
    cost_shopee_return_shipping: float = 0.0  # programa Devolução Fácil: sem cobrança na devolução

    sync_interval_minutes: int = 15
    sync_lookback_days: int = 30
    enable_scheduler: bool = True  # false no Cloud Run (usa Cloud Scheduler -> POST /jobs/run)
    webhook_process_inline: bool = False  # true no Cloud Run

    timezone: str = "America/Sao_Paulo"

    # Telas da equipe
    session_secret: str = ""  # vazio = derivado da API_KEY
    session_days: int = 14
    # Fotos/vídeos. local = pasta MEDIA_DIR (provisório: no Cloud Run o disco é temporário)
    # gcs = Google Cloud Storage no bucket GCS_BUCKET (usa a conta de serviço do Cloud Run)
    media_backend: str = "local"
    media_dir: str = "media"
    gcs_bucket: str = ""
    media_max_mb: int = 200
    backup_keep_days: int = 14  # backups diários do banco guardados (app/services/backup.py)

    # Avisos por e-mail (SMTP). Gmail: smtp.gmail.com:587 + "senha de app" da conta
    smtp_host: str = ""
    smtp_port: int = 587
    smtp_user: str = ""
    smtp_password: str = ""
    smtp_from: str = ""  # vazio = smtp_user
    alert_emails: str = ""  # destinatários separados por vírgula
    admin_emails: str = ""  # quem recebe aviso de falha nas integrações (vazio = ALERT_EMAILS)
    # Onde descobrir o IP de internet do servidor (vazio = não confere). A Shein só aceita IPs da lista branca.
    public_ip_url: str = "https://api.ipify.org"
    alert_digest_hours: str = "8,14"  # horários do resumo (fuso TIMEZONE)
    alert_urgent_hours: int = 12  # alerta quando faltar menos que isso para vencer
    # Endereço das telas usado nos links dos e-mails (vazio = PUBLIC_BASE_URL)
    app_url: str = ""


@lru_cache
def get_settings() -> Settings:
    return Settings()
