-- Retornos de mercadoria não entregue ao destinatário (volta pela transportadora).

alter table returns
  add column if not exists kind text not null default 'customer_return'
    check (kind in ('customer_return', 'undelivered'));

create index if not exists returns_kind_idx on returns (kind);
create index if not exists returns_erp_order_idx on returns (erp_order_id);

-- ID da integração (e-commerce) da loja dentro do Olist, para saber de qual loja é cada pedido.
alter table stores add column if not exists erp_ecommerce_id text;
create unique index if not exists stores_erp_ecommerce_idx on stores (erp_ecommerce_id) where erp_ecommerce_id is not null;
