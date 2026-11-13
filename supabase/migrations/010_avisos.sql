-- Avisos por e-mail (resumo 8h/14h e alerta de prazo): registra o que já foi enviado para não repetir.
create table if not exists alerts_sent (
  key text primary key,          -- ex.: digest:2026-10-16:08 | urgent:<return_id>:<prazo>
  sent_at timestamptz not null default now(),
  recipients text,
  summary text
);

alter table alerts_sent enable row level security;
