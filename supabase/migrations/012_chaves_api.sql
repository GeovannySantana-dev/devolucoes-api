-- Chaves de API de integração (somente leitura) para outros sistemas internos.
-- Só o hash SHA-256 da chave fica no banco; a chave aparece uma única vez, ao ser criada.
create table if not exists api_keys (
  id uuid primary key default gen_random_uuid(),
  name text not null,                 -- ex.: "ERP interno", "BI financeiro"
  prefix text not null,               -- começo da chave, para identificar (ex.: pdv_AbC12xYz)
  key_hash text not null unique,
  created_by text,
  created_at timestamptz not null default now(),
  last_used_at timestamptz,
  revoked_at timestamptz
);

alter table api_keys enable row level security;
