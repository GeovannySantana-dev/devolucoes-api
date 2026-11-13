-- Lotes de transferência matriz (SP) -> filial (MS) das peças de revenda conferidas
create table if not exists transfer_batches (
  id uuid primary key default gen_random_uuid(),
  number serial unique,
  status text not null default 'closed',          -- closed | invoiced | stocked | error
  closed_at timestamptz not null default now(),
  closed_by text,
  pieces integer not null default 0,
  amount numeric(12,2) not null default 0,
  erp_invoice_id text,                              -- NF de transferência (conta 1)
  erp_invoice_number text,
  erp_invoice_status text,
  ms_stock_posted_at timestamptz,                   -- entrada no estoque da filial (conta 2)
  ms_stock_result jsonb,
  last_error text,
  created_at timestamptz not null default now()
);
alter table returns add column if not exists transfer_batch_id uuid references transfer_batches(id) on delete set null;
create index if not exists returns_transfer_batch_idx on returns (transfer_batch_id);
alter table transfer_batches enable row level security;  -- como as demais tabelas (o app acessa direto pelo Postgres)
