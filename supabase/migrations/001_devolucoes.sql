-- Schema da API de devoluções. Rode no SQL Editor do Supabase (ou via `supabase db push`).

create extension if not exists pgcrypto;

create table if not exists stores (
  id uuid primary key default gen_random_uuid(),
  platform text not null check (platform in ('tiktok', 'shopee', 'mercado_livre', 'shein')),
  name text not null,
  external_shop_id text,
  shop_cipher text,
  region text,
  access_token text,
  refresh_token text,
  access_token_expires_at timestamptz,
  refresh_token_expires_at timestamptz,
  active boolean not null default true,
  last_synced_at timestamptz,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (platform, external_shop_id)
);

create table if not exists rejection_reasons (
  code text primary key,
  label text not null,
  active boolean not null default true,
  created_at timestamptz not null default now()
);

create table if not exists returns (
  id uuid primary key default gen_random_uuid(),
  store_id uuid not null references stores(id) on delete cascade,
  platform text not null,
  external_return_id text not null,
  external_order_id text,
  return_type text not null default 'unknown',
  status text not null,
  platform_status text,
  reason_code text,
  reason_text text,
  refund_amount numeric(12, 2),
  currency text,
  tracking_number text,
  carrier text,
  requested_at timestamptz,
  platform_updated_at timestamptz,
  action_deadline timestamptz,
  resolved_at timestamptz,
  decision text check (decision in ('approved', 'rejected')),
  decided_at timestamptz,
  decided_by text,
  platform_reject_reason_code text,
  platform_reject_reason_text text,
  internal_reject_reason_code text references rejection_reasons(code),
  reject_comment text,
  internal_notes text,
  raw jsonb,
  created_at timestamptz not null default now(),
  updated_at timestamptz not null default now(),
  unique (store_id, external_return_id)
);

create index if not exists returns_status_idx on returns (status);
create index if not exists returns_requested_at_idx on returns (requested_at);
create index if not exists returns_order_idx on returns (external_order_id);
create index if not exists returns_deadline_idx on returns (action_deadline) where status in ('pending_review', 'in_transit', 'received');

create table if not exists return_items (
  id uuid primary key default gen_random_uuid(),
  return_id uuid not null references returns(id) on delete cascade,
  external_item_id text,
  sku_id text,
  seller_sku text,
  product_name text,
  sku_name text,
  quantity integer not null default 1,
  refund_amount numeric(12, 2),
  image_url text,
  raw jsonb
);

create index if not exists return_items_return_idx on return_items (return_id);
create index if not exists return_items_sku_idx on return_items (seller_sku);

create table if not exists return_events (
  id uuid primary key default gen_random_uuid(),
  return_id uuid not null references returns(id) on delete cascade,
  event_type text not null,
  source text,
  from_status text,
  to_status text,
  actor text,
  message text,
  payload jsonb,
  created_at timestamptz not null default now()
);

create index if not exists return_events_return_idx on return_events (return_id, created_at);

create table if not exists webhook_events (
  id uuid primary key default gen_random_uuid(),
  platform text not null,
  store_id uuid references stores(id) on delete set null,
  notification_id text,
  event_type text,
  external_id text,
  payload jsonb,
  processed_at timestamptz,
  error text,
  received_at timestamptz not null default now(),
  unique (platform, notification_id)
);

-- Motivos internos de recusa (edite à vontade; usados nos relatórios)
insert into rejection_reasons (code, label) values
  ('produto_usado', 'Produto com sinais de uso'),
  ('produto_danificado_cliente', 'Produto danificado pelo cliente'),
  ('item_divergente', 'Item devolvido diferente do enviado'),
  ('item_faltando', 'Itens/acessórios faltando na devolução'),
  ('embalagem_violada', 'Embalagem original violada ou ausente'),
  ('fora_do_prazo', 'Solicitação fora do prazo de devolução'),
  ('sem_defeito', 'Defeito alegado não constatado'),
  ('evidencia_insuficiente', 'Evidências (fotos/vídeo) insuficientes'),
  ('outro', 'Outro motivo')
on conflict (code) do nothing;

-- IMPORTANTE: o Supabase expõe o schema public pela API REST com a anon key.
-- Ativar RLS sem políticas bloqueia esse acesso (inclusive aos tokens das lojas).
-- A API Python conecta como usuário do banco e não é afetada.
alter table stores enable row level security;
alter table rejection_reasons enable row level security;
alter table returns enable row level security;
alter table return_items enable row level security;
alter table return_events enable row level security;
alter table webhook_events enable row level security;
