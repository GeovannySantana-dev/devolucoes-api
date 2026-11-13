-- Situação do pacote de volta (logística reversa), consultada no rastreio da plataforma.
-- awaiting_shipment | in_transit | delivered | failed | lost | cancelled | none
alter table returns
  add column if not exists logistics_status text,
  add column if not exists logistics_detail text,        -- última movimentação ("Pedido devolvido")
  add column if not exists logistics_event_at timestamptz,
  add column if not exists logistics_checked_at timestamptz;

create index if not exists returns_logistics_status_idx on returns (logistics_status);
