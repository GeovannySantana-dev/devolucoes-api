-- Motivo da última falha de sincronização de cada loja (aviso na fila, em Integrações e por e-mail)
alter table stores add column if not exists last_sync_error text;
alter table stores add column if not exists sync_error_since timestamptz;
