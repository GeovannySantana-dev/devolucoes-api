-- Produtividade da bancada: o que cada operador fez e quando (bipagem, abrir, conferir) e o ponto do turno
create table if not exists operator_events (
  id bigserial primary key,
  user_id uuid not null references users(id) on delete cascade,
  kind text not null,          -- shift_start | pause | resume | shift_end | scan | open | inspect
  return_id uuid references returns(id) on delete set null,
  detail text,                 -- ex.: código bipado
  at timestamptz not null default now()
);
create index if not exists operator_events_user_at_idx on operator_events (user_id, at);
create index if not exists operator_events_at_idx on operator_events (at);
alter table operator_events enable row level security;
-- Custo da hora de cada pessoa (salário + encargos), informado pelo gestor, para o custo por pacote
alter table users add column if not exists hourly_cost numeric(10,2);
