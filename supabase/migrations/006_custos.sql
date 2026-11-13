-- Custo da devolução para a empresa (financeiro).
alter table returns
  add column if not exists liability text check (liability in ('seller', 'platform', 'buyer', 'none')),
  add column if not exists cost_return_shipping numeric(12, 2) not null default 0,
  add column if not exists cost_product_loss numeric(12, 2) not null default 0,
  add column if not exists cost_operational numeric(12, 2) not null default 0,
  add column if not exists cost_total numeric(12, 2) not null default 0,
  add column if not exists cost_computed_at timestamptz;

create index if not exists returns_liability_idx on returns (liability);
