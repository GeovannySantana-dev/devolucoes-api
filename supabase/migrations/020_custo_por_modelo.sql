-- Custo de cada peça por modelo (valores de exemplo, fictícios).
-- O SKU da devolução é ligado ao modelo pelo início do SKU; vale o início mais longo que casar
-- (ex.: VCA-PRETLIS = listrado, LT01; o resto de VCA = liso, BR01). SKU sem modelo usa COST_PRODUCT_DEFAULT.
create table if not exists product_cost_models (
  sku_prefix text primary key,
  model_code text not null,
  model_name text not null,
  unit_cost numeric(12, 2) not null check (unit_cost >= 0),
  updated_at timestamptz not null default now()
);

alter table product_cost_models enable row level security;

insert into product_cost_models (sku_prefix, model_code, model_name, unit_cost) values
  ('VR', 'LN01', 'Vestido em linho regata midi', 31.50),
  ('300', 'LN01', 'Vestido em linho regata midi', 31.50),
  ('VC', 'LN02', 'Vestido em linho regata curto', 27.40),
  ('VM', 'LN03', 'Vestido em linho manga curta midi', 36.20),
  ('CO', 'LN04', 'Conjunto em linho', 30.00),
  ('VRP', 'MT01', 'Vestido pespontado midi', 32.00),
  ('C-CPR', 'MT02', 'Conjunto pespontado calça+blusa', 38.00),
  ('VN', 'MT03', 'Vestido com nó', 25.00),
  ('VCA', 'BR01', 'Vestido com amarração liso curto', 28.00),
  ('VCA-CAQULIS', 'LT01', 'Vestido com amarração listrado curto', 29.00),
  ('VCA-PRETLIS', 'LT01', 'Vestido com amarração listrado curto', 29.00),
  ('VCA-ROSCLLI', 'LT01', 'Vestido com amarração listrado curto', 29.00),
  ('VMA', 'BR02', 'Vestido com amarração midi', 33.00),
  ('CSL', 'LT02', 'Conjunto Listrado', 37.00)
on conflict (sku_prefix) do update
  set model_code = excluded.model_code, model_name = excluded.model_name, unit_cost = excluded.unit_cost, updated_at = now();
