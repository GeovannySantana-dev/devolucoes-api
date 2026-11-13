-- Telas da equipe: usuários com login individual e fotos/vídeos das devoluções.

create table if not exists users (
  id uuid primary key default gen_random_uuid(),
  email text not null unique,
  name text not null,
  -- operador: confere o pacote, envia fotos/vídeos, aceita/recusa e gera a NF | gestor: tudo + usuários
  role text not null check (role in ('operador', 'gestor')),
  password_hash text not null,
  active boolean not null default true,
  last_login_at timestamptz,
  created_at timestamptz not null default now()
);

create table if not exists return_media (
  id uuid primary key default gen_random_uuid(),
  return_id uuid not null references returns(id) on delete cascade,
  kind text not null check (kind in ('photo', 'video')),
  -- inspection: tirada na conferência | buyer: enviada pelo comprador | other
  source text not null default 'inspection',
  storage_path text not null,
  content_type text,
  size_bytes bigint,
  original_name text,
  uploaded_by text,
  created_at timestamptz not null default now()
);

create index if not exists return_media_return_idx on return_media (return_id, created_at);
create index if not exists returns_tracking_idx on returns (tracking_number);

alter table users enable row level security;
alter table return_media enable row level security;
