# Guia técnico

> Manual de operação e detalhes de cada integração. A visão geral do projeto está no [README](../README.md).

Centraliza as devoluções das lojas (TikTok Shop, Shopee, Mercado Livre, Shein) em um único banco,
com recebimento em tempo real, aprovação/recusa, motivos de recusa e relatórios.

**Status das integrações:** TikTok Shop ✅ completo · Shopee ✅ 2 lojas · Mercado Livre ✅ (fotos na recusa ainda pelo painel) · Shein ⏳ (estrutura pronta, falta o adaptador)

## Como funciona

```
TikTok ──webhook──▶ /webhooks/tiktok ──▶ busca a devolução na API ──▶ Supabase
       ◀─sync a cada 15 min (rede de segurança caso algum webhook se perca)─┘
Operador ──▶ /returns/{id}/approve | /reject ──▶ TikTok API + histórico local
Painel/BI ──▶ /reports/*
```

- Cada marketplace é um **adaptador** (`app/platforms/`). O resto da API só conhece o modelo normalizado.
- Status unificado: `pending_review`, `awaiting_buyer_shipment`, `in_transit`, `received`, `completed`, `rejected`, `cancelled`. O status original fica em `platform_status`.
- Todo evento (criação, mudança de status, aprovação, recusa, falha, nota) fica em `return_events`.

## Telas da equipe (`/app`)

Páginas pensadas para o celular, servidas pela própria API (Jinja2, sem build). Login individual: cada
conferência, decisão e upload fica no histórico com o nome de quem fez.

| Tela | O que faz |
|---|---|
| Fila (`/app`) | Abas **Aguardando decisão**, **Para conferir** (a caminho ou entregue, ainda não conferida), **Vencidas**, **Erros no Olist** e **Todas**. A busca acha por rastreio, pedido, nº da devolução, nº do pedido no Olist ou SKU; um resultado só abre direto. Botão de câmera lê o código de barras da etiqueta (Chrome no Android, exige HTTPS) |
| Devolução (`/app/devolucoes/{id}`) | Itens, mídia do comprador (Shopee), **conferência** (revenda/defeito, fotos e vídeos, observações, gera a NF), **aceitar/recusar** no marketplace, Olist, notas e histórico |
| Usuários (`/app/usuarios`) | Só gestor: criar e desativar usuários |

**Conferência — 3 condições.** Regra da empresa: **toda devolução recebe NF de devolução; só a revenda volta ao
estoque.** `resale` (revenda: NF + estoque), `defect` (defeito: NF, sem estoque) e `mismatch` (**item diferente do
pedido**: veio outro produto, ex.: golpe — NF, sem estoque). Para `mismatch` a foto é obrigatória, o custo conta a
peça como perdida e a tela leva direto para contestar com o motivo interno "Item devolvido diferente do enviado".
`/reports/by-product` traz `mismatches` por SKU.
Migração: `008_item_diferente.sql`.

**Perfis:** `operador` (confere, envia mídia, aceita/recusa, gera NF) e `gestor` (tudo + usuários).
Criar o primeiro usuário (a senha é digitada sem aparecer):

```bash
.venv\Scripts\python scripts\criar_usuario.py voce@empresa.com "Seu Nome" gestor
```

**Sem deploy (provisório):** `deploy\agendar-servidor-windows.ps1` deixa as telas no ar neste PC, iniciando
no logon, para o celular acessar pelo Wi-Fi em `http://IP-DO-PC:8000/app` (a rede do Windows precisa estar como
Privada e a porta 8000 liberada no firewall). Remover depois do deploy: `-Remover`.

**Fotos e vídeos** (`app/services/media.py`): o celular reduz as fotos para no máx. 1920 px em JPEG antes de
enviar (2-4 MB → ~300 KB). `MEDIA_BACKEND=local` guarda em `MEDIA_DIR` (padrão `media/`, provisório);
`MEDIA_BACKEND=gcs` guarda no Google Cloud Storage (`GCS_BUCKET`, privado, servido pela própria API). O
`deploy-cloud-run.ps1` cria o bucket, dá acesso à conta do Cloud Run, copia as mídias deste PC
(`scripts\migrar_midias_gcs.py`) e publica já com `gcs`. Limite `MEDIA_MAX_MB` (200) por arquivo.
Migração do banco: `007_usuarios_midias.sql`.

## Avisos por e-mail

`app/services/alerts.py`, dentro das rotinas (a cada 15 min):
- **Resumo** nos horários de `ALERT_DIGEST_HOURS` (padrão 8h e 14h): vencidas, vencendo em 24h, aguardando
  decisão, pacotes entregues para conferir e erros no Olist, com links para as telas.
- **Alerta urgente** quando uma devolução que a loja pode decidir agora (aguardando decisão / pacote entregue)
  ficar a menos de `ALERT_URGENT_HOURS` (12) do prazo. Uma vez por devolução e prazo.

O que foi enviado fica em `alerts_sent` (migração `010`). Configuração: `SMTP_*`, `ALERT_EMAILS`, `APP_URL`.
Teste: `.venv\Scripts\python scripts\testar_email.py`.

## Operação e segurança

- **Limpeza da fila** (`/app/limpeza`, gestor): marca as devoluções físicas não conferidas até uma data como
  "tratadas fora do sistema" (processo antigo). Saem de "Para conferir", sem NF/estoque/marketplace; cada uma
  pode voltar à fila pelo detalhe. Migração `011`.
- **Backup diário** (`app/services/backup.py`): JSON compactado de todas as tabelas (sem tokens das lojas),
  em `backups/auto/` ou no bucket (`backups/`) com `MEDIA_BACKEND=gcs`; guarda `BACKUP_KEEP_DAYS` (14).
- **Aviso de falha** (`app/services/health.py`): no fim das rotinas, e-mail para `ADMIN_EMAILS` (ou
  `ALERT_EMAILS`) se uma rotina falhar, uma loja ficar 3 h sem sincronizar ou a autorização vencer em 3 dias.
  Cada problema, uma vez por dia.
- **Fotos na recusa:** Shopee e Mercado Livre anexam até 3 fotos da conferência. TikTok também, mas exige o
  escopo `seller.product.basic` no app; sem ele a recusa vai só com o comentário e a tela avisa.
- **Guia do operador:** `/app/ajuda`.
- **Tokens cifrados:** `access_token`/`refresh_token` das lojas ficam cifrados no banco (Fernet, `app/crypto.py`)
  com `TOKEN_ENCRYPTION_KEY`. Valores antigos em texto puro continuam legíveis; `scripts\criptografar_tokens.py`
  cifra os existentes. Perder a chave = autorizar todas as lojas de novo.

## Shopee

Sem webhook: a sincronização periódica busca as devoluções (janelas de até 15 dias, paginadas).

- **Status:** com o Devolução Fácil a Shopee aceita (`ACCEPTED`) antes de o pacote chegar, então a devolução
  fica `completed` (reembolso resolvido) e o físico é acompanhado pela conferência (`received_at`) e pelo
  rastreio. `platform_status` traz o status e a logística (ex.: `ACCEPTED / LOGISTICS_PICKUP_DONE`).
  `REQUESTED` e `PROCESSING` com `negotiation_status=PENDING_RESPOND` → aguardando o vendedor;
  `PROCESSING` sem negociação pendente → `awaiting_buyer_shipment` (a loja valida na chegada do pacote);
  `JUDGING`/`SELLER_DISPUTE` → `disputed`.
- **Aprovar** = `returns/confirm`. **Recusar** = contestar (`returns/dispute`, campo `dispute_reason_id`): o
  `platform_reason_code` vem de `GET /returns/{id}/reject-reasons` e é obrigatório `SHOPEE_DISPUTE_EMAIL`.
- **Evidências:** quase todo motivo exige fotos. A API envia as até 3 fotos mais recentes da conferência (JPG/PNG
  até 10 MB; vídeos não são aceitos por essa via) com `returns/convert_image` e as anexa em cada módulo de
  evidência obrigatório. Sem foto, a contestação é bloqueada com aviso.
- **Rastreio reverso** (`app/services/tracking.py`): a cada rodada de `/jobs/run`, até 150 devoluções físicas
  da Shopee ainda não conferidas (últimos 45 dias) têm o rastreio consultado em `get_reverse_tracking_info`
  (de novo a cada 3 h, até "entregue"). Fica em `logistics_status` (`awaiting_shipment`, `in_transit`,
  `delivered`, `failed`, `lost`, `cancelled`, `none` = ainda sem postagem), `logistics_detail` (última
  movimentação) e `logistics_event_at`. A fila "Para conferir" usa isso: entregues primeiro. Migração `009`.
- Dá para contestar **mesmo depois de a Shopee aceitar** (`ACCEPTED`, Devolução Fácil): a tela mostra
  "Contestar devolução" nesses casos (ex.: item diferente do pedido).
- O `access_token` dura 4 h e o `refresh_token` 30 dias: a API renova sozinha, mas se ficar mais de
  30 dias sem rodar será preciso autorizar de novo.

**Autorizar a loja:** preencha `SHOPEE_PARTNER_ID`/`SHOPEE_PARTNER_KEY` e rode
`.venv\Scripts\python scripts\shopee_autorizar.py` (cola-se a URL de retorno), ou, com a API publicada,
use `GET /auth/shopee/authorize-url`. Depois: `.venv\Scripts\python scripts\sincronizar.py shopee`.

## Mercado Livre

Cada devolução é uma **reclamação** (`/post-purchase/v1/claims`, a loja como `respondent`), com a devolução
física (`/post-purchase/v2/claims/{id}/returns`) e o pedido. Cancelamentos antes do envio (`cancel_purchase`)
são ignorados. A sync percorre as reclamações pela data de atualização; o webhook acelera.

- **Status:** fechada a favor do comprador → `completed`; a favor da loja → `rejected`; sem beneficiado →
  `cancelled`; em mediação (`stage=dispute`) → `disputed`; aberta com devolução → pelo status do envio
  (`label_generated` → aguardando envio, `shipped` → a caminho, `delivered` → recebida); aberta sem devolução e
  com ação da loja → `pending_review`. `platform_status` traz `tipo:etapa:status / return:status`.
- **Itens:** só os itens devolvidos (`return.orders`); sem devolução física, o pedido inteiro. O SKU vem do
  pedido ou, em pedidos antigos sem `seller_sku`, do atributo `SELLER_SKU` da variação do anúncio.
- **Olist:** o vínculo tenta o nº do pedido e, se não achar, o `pack_id` (carrinho com vários pedidos).
- **Aprovar** (`POST /returns/{id}/approve`) escolhe a ação disponível na reclamação, nesta ordem:
  `return_review_ok` (pacote chegou e está certo) → `refund` (reembolso total) → `allow_return` (gera a
  etiqueta para o comprador). Dá para forçar com `decision`.
- **Recusar** (`POST /returns/{id}/reject`): os motivos de `GET /returns/{id}/reject-reasons` são os da
  revisão com problema (`SRF2`…`SRF7`, exigem `comment`) e `open_dispute` (pede mediação). `SRF2` (avariado) e
  `SRF4` (produto diferente) exigem fotos: por enquanto, faça esses pelo painel do Mercado Livre.
- **Webhook:** `POST /webhooks/mercadolivre` — no app, marque o tópico de **reclamações** e use
  `{PUBLIC_BASE_URL}/webhooks/mercadolivre`. Todas as notificações ficam em `webhook_events`; as de
  reclamação atualizam a devolução. Avisos não processados são retomados em `POST /jobs/run`.
- O Mercado Livre limita a taxa de chamadas: o cliente espaça as chamadas e espera no 429.

**Autorizar a conta** (precisa ser o **titular**, não colaborador): `.venv\Scripts\python scripts\mercadolivre_autorizar.py`
gera o link; depois de Permitir, rode de novo passando a URL da barra de endereço entre aspas.

## Integração com o Olist ERP

O Olist continua sendo onde as notas são emitidas e onde fica o estoque. A API automatiza o caminho até lá:

```
Devolução chega ──▶ vincula ao pedido e à NF de venda no Olist (automático)
                ──▶ sugere a condição pelo motivo do comprador (tamanho/cor → revenda; defeito → defeito)
Pacote chega   ──▶ POST /returns/{id}/receive  {"condition": "resale" | "defect"}   ← conferência confirma
                ──▶ cria a NF de entrada de devolução no Olist (referenciando a NF de venda, só os itens devolvidos)
NF autorizada  ──▶ resale: lança estoque   |   defect: NÃO lança estoque
```

- **`OLIST_INVOICE_MODE=draft`** (padrão): a NF fica como rascunho no Olist e a equipe confere e emite lá.
  A API verifica a cada 10 min e, quando a NF é autorizada, processa o estoque.
- **`emit`**: a API já transmite a NF. **`off`**: a API só vincula e registra a condição.
- Se a sua natureza de operação de devolução já lança estoque automaticamente no Olist, use
  `OLIST_POST_STOCK=false` e cadastre uma natureza separada **sem** lançamento de estoque para defeitos
  (`OLIST_NATUREZA_DEFECT_ID`). Senão os defeituosos entrariam no estoque.
- O SKU do marketplace precisa ser igual ao código do produto no Olist (é assim que os itens são casados).
- As regras de sugestão ficam em `/return-reason-rules` (palavra-chave → condição) e podem ser editadas.

> **CFOP:** a API do Olist não aceita CFOP na nota; ele vem da natureza. O app lê o CFOP de cada item da NF de venda
> e escolhe a natureza de devolução em `OLIST_RETURN_NATUREZAS` (5102/6102/6108 → `202`, 5101/6101/6107 → `201`,
> 5405/6404 → `411`; venda emitida no Simples → sufixo `S`, sem destaque de impostos). Cada natureza no Olist usa
> CFOP `?.202` (o Olist põe 1 ou 2 pela UF), espelha CST/alíquota da venda e PIS/COFINS CST 50 (1,65% / 7,6%), série 4.

> ⚠️ Valide com a contabilidade a natureza de operação, CFOP e finalidade (padrão 4 = devolução) antes de
> usar o modo `emit`. Comece em `draft` e confira as primeiras notas geradas.

| Método | Rota | Descrição |
|---|---|---|
| POST | `/returns/{id}/receive` | Conferência física: condição + observações; gera a NF |
| POST | `/returns/{id}/condition` | Corrige a condição (antes de criar a NF) |
| POST | `/returns/{id}/erp/link` | Vincula ao pedido/NF de venda no Olist |
| POST | `/returns/{id}/erp/invoice` | Cria a NF de devolução (ou recria após cancelamento) |
| POST | `/returns/{id}/erp/emit` | Transmite a NF para a SEFAZ |
| POST | `/returns/{id}/erp/refresh` | Consulta a situação da NF e processa o estoque |
| POST | `/returns/{id}/erp/stock` | Processa o estoque manualmente |
| GET | `/reports/by-condition` | Revenda x defeito, NFs pendentes, estoque lançado |

Filtros novos em `GET /returns`: `condition`, `erp_invoice_status` (`none`/`draft`/`authorized`/`cancelled`), `has_erp_error`.
O relatório `/reports/by-product` agora traz `defects` e `defect_rate` por SKU.

### Retorno de mercadoria não entregue

Pacotes que voltam pela transportadora sem chegar ao cliente (endereço não encontrado, recusa etc.)
não são devolução de venda: usam a natureza **"Retorno de mercadoria não entregue ao destinatário"**.

- A cada 30 min a API busca no Olist os pedidos com situação **Não entregue** (das 6 lojas, inclusive
  Shopee, Mercado Livre e Shein) e cria o registro com `kind=undelivered`, já vinculado ao pedido e à NF de venda.
- Na chegada do pacote, a mesma conferência `POST /returns/{id}/receive` gera a NF de retorno
  (pedido inteiro) e o estoque segue a regra de sempre: revenda volta, avaria não.
- Se o pedido ainda não estiver como "Não entregue" no Olist, registre à mão: `POST /returns/undelivered`.
- **Pelas telas** (`/app/nao-entregue`, botão "Registrar pacote não entregue" na fila): o operador digita ou lê
  a **chave da NF** (código de barras do DANFE), o nº do pedido do marketplace ou o nº do pedido no Olist. O
  sistema acha o pedido no Olist, descobre a loja pelo canal (`erp_ecommerce_id`) e abre a conferência. Se já
  existir devolução para o pedido, abre a existente.
- Filtre com `GET /returns?kind=undelivered`; os relatórios aceitam `kind` e o resumo traz `by_kind`.

**Vincular as lojas ao Olist (uma vez):** rode `POST /erp/import-undelivered`. Se vier algo em
`unmapped` (ID e nome da integração no Olist), associe à loja com `PATCH /stores/{id}` `{"erp_ecommerce_id": "<ID>"}`.
Lojas sem integração na API (Shopee, ML, Shein) podem ser criadas com `POST /stores`.

### Custo das devoluções (financeiro)

Cada devolução recebe o custo para a empresa, recalculado sempre que muda:

| Componente | Regra |
|---|---|
| Frete de devolução | TikTok: valor exato pago pela loja (`seller_paid_return_shipping_fee`). Shopee: R$ 0 (Devolução Fácil). Mercado Livre: valor de `/claims/{id}/charges/return-cost` |
| Perda da peça | `COST_PRODUCT_DEFAULT` × unidades, só quando a **conferência** confirmou **defeito** ou **item diferente** (a sugestão automática pelo motivo não conta) |
| Operacional | `COST_OPERATIONAL_PER_RETURN` por devolução com retorno físico |
| Venda perdida | Pelo **financeiro** de cada plataforma (TikTok: extrato; Shopee: `payment/get_escrow_detail`; Mercado Livre: faturamento, cobranças menos estornos), depois que a devolução fecha: frete de ida que a loja pagou (ou desconto de frete estornado) e taxas que não voltam (comissão, afiliado, serviço) |

> **Estimado x confirmado (TikTok, Shopee, Mercado Livre).** Shopee: frete de devolução = `reverse_shipping_fee` do repasse (a Devolução Fácil nem sempre cobre: em "produto errado" a loja paga R$ 15). Mercado Livre: frete de ida e tarifas não estornadas pelo faturamento. TikTok: Enquanto a devolução está aberta, o frete é a previsão que vem na devolução. Cerca de 12 h depois de fechar, a rotina `tiktok_extrato` lê `/finance/202501/orders/{id}/statement_transactions` e grava o valor real (`settled_*`, `cost_settled_at`, migração `013`). No Excel, a coluna "Custo" diz se é Estimado ou Confirmado (extrato).

`liability` indica quem arcou com o frete: `seller` (culpa da loja: cor/item errado, defeito, não conforme),
`platform` (tamanho, arrependimento) ou `buyer`. Reembolso não entra no custo; aparece como `refunded_amount`.

`GET /reports/costs?group_by=reason|liability|month|store|sku|condition`

Após mudar os valores no `.env`, recalcule o histórico com `scripts/reclassificar.py`.

## Configuração

1. **Banco:** rode no SQL Editor do Supabase, em ordem, os arquivos de `supabase/migrations/` (001 a 006).
2. **Variáveis:** copie `.env.example` para `.env` e preencha.
3. **Instalar e rodar:**
   ```bash
   python -m venv .venv
   .venv\Scripts\pip install -r requirements.txt
   .venv\Scripts\uvicorn app.main:app --reload
   ```
   Documentação interativa: http://localhost:8000/docs
4. **TikTok Shop Partner Center** (no seu app):
   - Redirect URL: `{PUBLIC_BASE_URL}/auth/tiktok/callback`
   - Webhook URL: `{PUBLIC_BASE_URL}/webhooks/tiktok`, assinando o evento **Return status change**
   - Escopos: Return & Refund e Shop Authorization
5. **Autorizar as 2 lojas:** chame `GET /auth/tiktok/authorize-url`, abra o link logado na loja 1, repita para a loja 2.
6. **Carga inicial:** `POST /sync` (busca os últimos `SYNC_LOOKBACK_DAYS` dias).

> **Sem a API publicada:** `deploy\agendar-rotinas-windows.ps1` agenda `scripts\rotinas.py` (o mesmo que
> `POST /jobs/run`) a cada 15 min no Agendador de Tarefas do Windows, com log em `logs\rotinas.log`. Mantém os
> tokens renovados e as devoluções sincronizadas. Depois do deploy: `deploy\agendar-rotinas-windows.ps1 -Remover`.

> Webhooks e OAuth exigem URL pública com HTTPS. Para testar local, use um túnel (ngrok, Cloudflare Tunnel).

### Computador servidor (antes da nuvem)

Um PC dedicado, sempre ligado, roda o app e o publica com HTTPS e endereço fixo pelo **Tailscale Funnel**
(grátis, `https://<pc>.<tailnet>.ts.net`). O banco continua no Supabase, então a ida para o Cloud Run depois
não perde nada.

1. No PC atual: `deploy\empacotar-para-servidor.ps1` gera `devolucoes-servidor.zip` na Área de Trabalho
   (código, `.env`, mídias e backups; sem `.venv`). O `.env` tem senhas: apague do pendrive depois.
2. No servidor: extraia, por exemplo em `C:\Devolucoes\devolucoes-api`, e rode
   `powershell -ExecutionPolicy Bypass -File .\deploy\instalar-servidor-windows.ps1`. Ele instala o Python 3.12
   e o Tailscale, cria o `.venv`, agenda as rotinas e o servidor, ativa o Funnel na porta 8000 e grava o
   endereço em `PUBLIC_BASE_URL`/`APP_URL`.
3. No PC antigo: `deploy\instalar-servidor-windows.ps1 -Remover`, para não sincronizar em dobro.
4. Cadastre o endereço novo nos marketplaces (callback e webhook do TikTok, domínio de redirect da Shopee,
   URI do Mercado Livre).

**Atualizar o servidor:** o código fica no GitHub. Depois de enviar as mudanças,
rode no servidor `powershell -ExecutionPolicy Bypass -File .\deploy\atualizar-servidor.ps1`. Na primeira vez,
acrescente `-Repo https://github.com/<conta>/devolucoes-api.git`. O script busca a versão nova,
instala as dependências, reinicia as telas e avisa se há migrações do Supabase para aplicar. O `.env`, as mídias
e os backups não vão para o GitHub.

O servidor precisa ficar sem suspender e com o usuário logado (as tarefas iniciam no logon). O login das
telas bloqueia o e-mail por 15 min depois de 5 senhas erradas seguidas.

## Endpoints

Todos (exceto webhook e callback OAuth) exigem o header `X-API-Key`.

> **Outros sistemas internos** não usam a chave principal: usam uma chave de integração **somente leitura**
> (`pdv_...`), criada em **Menu → Integrações (API)** (`/app/integracoes`, migração `012`), com as rotas
> `/integracao/v1/painel`, `/custos` e `/devolucoes`. Guia para quem integra: [api-integracao.md](api-integracao.md).

| Método | Rota | Descrição |
|---|---|---|
| GET | `/returns` | Lista com filtros: `platform`, `store_id`, `status`, `needs_action`, `overdue`, `decision`, `q` (devolução/pedido/SKU), `date_from`, `date_to`, `sort`, paginação |
| GET | `/returns/{id}` | Detalhe com itens e histórico |
| GET | `/returns/{id}/reject-reasons` | Motivos aceitos pela plataforma + catálogo interno |
| POST | `/returns/{id}/approve` | Aprova (escolhe automaticamente a decisão certa para a etapa) |
| POST | `/returns/{id}/reject` | Recusa com motivo da plataforma + motivo interno + comentário |
| POST | `/returns/{id}/notes` | Nota interna |
| POST | `/returns/{id}/refresh` | Relê da plataforma |
| GET/PUT | `/rejection-reasons` | Catálogo interno de motivos de recusa |
| GET | `/reports/summary` | Totais, pendentes, atrasadas, taxa de aprovação, valor reembolsado, tempo médio de resolução |
| GET | `/reports/by-reason` | Motivos alegados pelos compradores |
| GET | `/reports/by-product` | SKUs mais devolvidos |
| GET | `/reports/by-store` | Comparativo entre lojas |
| GET | `/reports/rejections` | Recusas por motivo interno, motivo da plataforma e operador |
| GET | `/reports/timeseries` | Evolução por dia/semana/mês |
| GET/POST/PATCH | `/stores` | Lojas |
| POST | `/sync`, `/stores/{id}/sync?full=true` | Sincronização manual |

### Exemplo: recusar uma devolução

```bash
# 1. Ver motivos aceitos
curl -H "X-API-Key: $KEY" http://localhost:8000/returns/<id>/reject-reasons

# 2. Recusar
curl -X POST -H "X-API-Key: $KEY" -H "Content-Type: application/json" \
  http://localhost:8000/returns/<id>/reject \
  -d '{"platform_reason_code": "<code do passo 1>", "internal_reason_code": "produto_usado",
       "comment": "Produto devolvido com sinais de uso", "actor": "maria@loja.com"}'
```

## Testes

```bash
.venv\Scripts\python -m pytest -q
```

## Próximos passos

- Validar o adaptador Shopee com a loja real; rastreio reverso (`get_reverse_tracking_info`) no histórico.
- Mercado Livre: anexar fotos na revisão com problema (SRF2/SRF4) e reembolso parcial.
- Adaptador Shein.
- Upload de fotos de evidência na recusa (TikTok aceita `images`).
- Cancelamentos (TikTok webhook tipo 11) no mesmo fluxo.
- Login por usuário (hoje é uma API key única; `actor` é informado no corpo).
