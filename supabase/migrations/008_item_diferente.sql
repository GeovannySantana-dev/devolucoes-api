-- Conferência: item diferente do pedido (veio outro produto no pacote, ex.: tentativa de golpe).
-- Não gera NF de devolução nem estoque; as fotos servem de prova para contestar no marketplace.
alter table returns drop constraint if exists returns_item_condition_check;
alter table returns add constraint returns_item_condition_check
  check (item_condition in ('resale', 'defect', 'mismatch'));

alter table returns drop constraint if exists returns_erp_stock_status_check;
alter table returns add constraint returns_erp_stock_status_check
  check (erp_stock_status in ('posted', 'skipped_defect', 'skipped_mismatch', 'external'));
