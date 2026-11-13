-- Motivo do defeito na conferência (base da NF de baixa por perda, CFOP 5.927)
alter table returns add column if not exists defect_reason text;

create index if not exists returns_defect_received_idx
  on returns (received_at) where item_condition = 'defect';
