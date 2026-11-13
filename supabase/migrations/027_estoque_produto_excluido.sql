-- NF de devolução de produto excluído no cadastro do Olist: estoque não lançado (e a rotina para de tentar).
alter table returns drop constraint returns_erp_stock_status_check;
alter table returns add constraint returns_erp_stock_status_check
  check (erp_stock_status = any (array['posted','skipped_defect','skipped_mismatch','skipped_no_product','external','reserved','launched']));
