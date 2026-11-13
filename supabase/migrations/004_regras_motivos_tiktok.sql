-- Regras extras para os motivos do TikTok (vêm em inglês) e textos livres do histórico da Shopee.
insert into return_reason_rules (keyword, condition, priority) values
  ('fit', 'resale', 50),
  ('description', 'resale', 60),
  ('fabric', 'resale', 60),
  ('material', 'resale', 60),
  ('style', 'resale', 60),
  ('late', 'resale', 60),
  ('enorme', 'resale', 50),
  ('mancha', 'defect', 10),
  ('manchas', 'defect', 10),
  ('desfiado', 'defect', 10),
  ('fios puxados', 'defect', 10),
  ('furo', 'defect', 10),
  ('riscado', 'defect', 10)
on conflict (keyword) do nothing;
