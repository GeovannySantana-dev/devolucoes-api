-- Nome da loja escolhido pela equipe (vazio = nome da plataforma, que continua em "name")
alter table stores add column if not exists display_name text;
-- Comprador da devolução, para avisar reincidência. Só Shopee (usuário) e Mercado Livre (id do comprador):
-- TikTok e Shein não informam o comprador na devolução.
alter table returns add column if not exists buyer_key text;
create index if not exists returns_buyer_key_idx on returns (buyer_key) where buyer_key is not null;
update returns set buyer_key = 'shopee:' || lower(raw->'user'->>'username')
  where platform = 'shopee' and buyer_key is null and coalesce(raw->'user'->>'username', '') <> '';
update returns set buyer_key = 'mercado_livre:' || coalesce(raw->'order'->'buyer'->>'id',
    (select p->>'user_id' from jsonb_array_elements(raw->'claim'->'players') p where p->>'role' = 'complainant' limit 1))
  where platform = 'mercado_livre' and buyer_key is null
    and coalesce(raw->'order'->'buyer'->>'id',
      (select p->>'user_id' from jsonb_array_elements(raw->'claim'->'players') p where p->>'role' = 'complainant' limit 1)) is not null;
