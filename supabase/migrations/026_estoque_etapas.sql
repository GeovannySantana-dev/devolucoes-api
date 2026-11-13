-- Etapas do lançamento de estoque da NF de devolução (o Olist lança a nota sempre no depósito padrão):
-- reserved = a peça já saiu do depósito de venda; launched = nota lançada, falta a entrada no depósito certo.
alter table returns drop constraint returns_erp_stock_status_check;
alter table returns add constraint returns_erp_stock_status_check
  check (erp_stock_status = any (array['posted','skipped_defect','skipped_mismatch','external','reserved','launched']));
