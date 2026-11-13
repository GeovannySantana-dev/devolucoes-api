-- Integração com o Olist ERP: vínculo com pedido/nota, condição do item e controle de estoque.

alter table returns
  add column if not exists item_condition text check (item_condition in ('resale', 'defect')),
  add column if not exists condition_source text check (condition_source in ('auto', 'manual')),
  add column if not exists received_at timestamptz,
  add column if not exists received_by text,
  add column if not exists inspection_notes text,
  add column if not exists erp_order_id text,
  add column if not exists erp_order_number text,
  add column if not exists erp_sale_invoice_id text,
  add column if not exists erp_sale_invoice_key text,
  add column if not exists erp_return_invoice_id text,
  add column if not exists erp_return_invoice_number text,
  add column if not exists erp_return_invoice_status text
    check (erp_return_invoice_status in ('draft', 'authorized', 'cancelled')),
  add column if not exists erp_stock_status text
    check (erp_stock_status in ('posted', 'skipped_defect')),
  add column if not exists erp_stock_posted_at timestamptz,
  add column if not exists erp_last_error text;

create index if not exists returns_erp_invoice_status_idx on returns (erp_return_invoice_status);
create index if not exists returns_item_condition_idx on returns (item_condition);

-- Regras para sugerir a condição a partir do motivo alegado pelo comprador.
-- A sugestão é confirmada na conferência física (POST /returns/{id}/receive).
create table if not exists return_reason_rules (
  keyword text primary key,
  condition text not null check (condition in ('resale', 'defect')),
  priority integer not null default 100
);

insert into return_reason_rules (keyword, condition, priority) values
  ('defeito', 'defect', 10),
  ('defeituoso', 'defect', 10),
  ('danificado', 'defect', 10),
  ('quebrado', 'defect', 10),
  ('avaria', 'defect', 10),
  ('não funciona', 'defect', 10),
  ('damaged', 'defect', 10),
  ('defective', 'defect', 10),
  ('broken', 'defect', 10),
  ('quality', 'defect', 20),
  ('qualidade', 'defect', 20),
  ('rasgado', 'defect', 10),
  ('costura', 'defect', 10),
  ('manchado', 'defect', 10),
  ('furado', 'defect', 10),
  ('zíper', 'defect', 10),
  ('ziper', 'defect', 10),
  ('não serviu', 'resale', 50),
  ('nao serviu', 'resale', 50),
  ('grande', 'resale', 50),
  ('pequeno', 'resale', 50),
  ('apertado', 'resale', 50),
  ('folgado', 'resale', 50),
  ('tamanho', 'resale', 50),
  ('size', 'resale', 50),
  ('cor', 'resale', 50),
  ('color', 'resale', 50),
  ('colour', 'resale', 50),
  ('errado', 'resale', 60),
  ('wrong', 'resale', 60),
  ('arrependimento', 'resale', 60),
  ('desistência', 'resale', 60),
  ('no longer', 'resale', 60),
  ('not needed', 'resale', 60)
on conflict (keyword) do nothing;

alter table return_reason_rules enable row level security;
