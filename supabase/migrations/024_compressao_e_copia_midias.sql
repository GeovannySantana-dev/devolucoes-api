-- Vídeos comprimidos no servidor (original_size_bytes = tamanho antes) e cópia das mídias no Supabase Storage
alter table return_media add column if not exists compressed_at timestamptz;
alter table return_media add column if not exists original_size_bytes bigint;
alter table return_media add column if not exists backup_at timestamptz;
create index if not exists return_media_sem_copia on return_media (created_at) where backup_at is null;
-- Bucket privado da cópia (só a chave secreta do servidor grava e lê)
insert into storage.buckets (id, name, public) values ('midias', 'midias', false) on conflict (id) do nothing;
