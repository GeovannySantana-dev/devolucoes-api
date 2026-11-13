-- Busca da fila pelo rastreio bipado: a comparação é feita em maiúsculas
create index if not exists returns_tracking_upper_idx on returns (upper(tracking_number));
create index if not exists return_items_sku_upper_idx on return_items (upper(seller_sku));
