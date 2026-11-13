-- Devoluções tratadas fora do sistema (processo antigo, antes de a equipe usar as telas):
-- saem da fila "Para conferir" sem gerar NF nem mexer no estoque.
alter table returns
  add column if not exists handled_outside_at timestamptz,
  add column if not exists handled_outside_by text;

create index if not exists returns_handled_outside_idx on returns (handled_outside_at);
