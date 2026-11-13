-- NF de devolução emitida fora da API (à mão no Olist): estoque tratado por quem emitiu.
alter table returns drop constraint if exists returns_erp_stock_status_check;
alter table returns add constraint returns_erp_stock_status_check
  check (erp_stock_status in ('posted', 'skipped_defect', 'external'));
