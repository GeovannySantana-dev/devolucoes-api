-- O app do TikTok é privado: só autoriza a loja do próprio parceiro. Cada loja guarda qual app ela usa
alter table stores add column if not exists tiktok_app smallint not null default 1;
