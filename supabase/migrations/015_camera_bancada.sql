-- Câmera da bancada: início da conferência (para recortar o vídeo) e situação do recorte
alter table returns
  add column if not exists inspection_started_at timestamptz,  -- operador abriu a devolução para conferir
  add column if not exists camera_clip_at timestamptz,         -- recorte feito ou tentado (não tenta de novo)
  add column if not exists camera_clip_error text;             -- por que não deu para recortar (ex.: sem gravação)
