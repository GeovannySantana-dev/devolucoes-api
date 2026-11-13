-- Lote de volta MS -> SP: peças de devolução que estavam no estoque da filial e voltam para venda na matriz.
-- Usa a mesma tabela dos lotes de ida: direction diz o sentido.
alter table transfer_batches add column if not exists direction text not null default 'to_ms';  -- to_ms | to_sp
alter table transfer_batches add column if not exists sp_stock_posted_at timestamptz;          -- entrada em SP (volta)
alter table returns add column if not exists back_batch_id uuid references transfer_batches(id) on delete set null;
create index if not exists returns_back_batch_idx on returns (back_batch_id);
