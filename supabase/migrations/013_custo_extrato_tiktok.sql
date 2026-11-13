-- Custo real das devoluções do TikTok, lido do extrato financeiro (finance/statement_transactions)
-- depois que a devolução fecha e o pedido é liquidado.
alter table returns
  add column if not exists cost_lost_sale numeric(12,2) not null default 0,   -- frete de ida e taxas da venda perdidos
  add column if not exists settled_return_shipping numeric(12,2),            -- frete de devolução segundo o extrato
  add column if not exists settled_lost_sale numeric(12,2),                  -- frete de ida + taxas não devolvidas, segundo o extrato
  add column if not exists cost_settled_at timestamptz,                      -- quando o extrato foi lido (null = custo estimado)
  add column if not exists cost_settle_checked_at timestamptz;               -- última tentativa de ler o extrato
