-- Rastreio do envio da venda (ida), vindo do pedido no Olist. Muitos clientes devolvem no mesmo pacote,
-- com a etiqueta de ida: a busca da fila também procura por este código.
alter table returns add column if not exists sale_tracking_number text;
create index if not exists returns_sale_tracking_idx on returns (upper(sale_tracking_number));
