-- "Sair de todos os aparelhos": a sessão carrega esta versão; somar 1 derruba todas as sessões do usuário.
alter table users add column if not exists session_version integer not null default 0;
