# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Backup das tabelas de um projeto Supabase para arquivos JSON locais.

Uso (PowerShell):
    $env:OLD_DATABASE_URL = "postgresql://postgres.xxxx:SENHA@aws-0-....pooler.supabase.com:5432/postgres"
    .venv\\Scripts\\python scripts\\backup_supabase.py

A connection string fica em Project Settings > Database > Connect (Session pooler).
Colunas com tokens/segredos não são copiadas.
"""

import json
import os
import sys
from datetime import datetime
from pathlib import Path

import psycopg

# Colunas que não devem ir para o backup (credenciais)
SECRET_COLUMNS = {"access_token", "refresh_token", "partner_key", "app_secret", "app_key"}


def main() -> None:
    url = os.environ.get("OLD_DATABASE_URL")
    if not url:
        sys.exit("Defina a variável OLD_DATABASE_URL com a connection string do projeto antigo.")
    url = url.replace("postgresql+psycopg://", "postgresql://")

    print("Conectando ao banco...", flush=True)
    try:
        conn = psycopg.connect(url, prepare_threshold=None, connect_timeout=20)
    except psycopg.OperationalError as exc:
        sys.exit(
            f"\nNão foi possível conectar: {str(exc).splitlines()[0]}\n"
            "Dica: use a string do 'Session pooler' (termina em pooler.supabase.com), "
            "que funciona em qualquer rede."
        )
    print("Conectado. Copiando tabelas...", flush=True)

    out = Path("backups") / f"central-devolucao-{datetime.now():%Y%m%d-%H%M%S}"
    out.mkdir(parents=True)

    manifest = {"created_at": datetime.now().isoformat(), "tables": {}}
    with conn:
        tables = [
            r[0]
            for r in conn.execute(
                "select table_name from information_schema.tables "
                "where table_schema = 'public' and table_type = 'BASE TABLE' order by 1"
            )
        ]
        for table in tables:
            columns = [
                r[0]
                for r in conn.execute(
                    "select column_name from information_schema.columns "
                    "where table_schema = 'public' and table_name = %s order by ordinal_position",
                    (table,),
                )
            ]
            kept = [c for c in columns if c not in SECRET_COLUMNS]
            cols_sql = ", ".join(f'"{c}"' for c in kept)
            rows = conn.execute(
                f'select coalesce(json_agg(t), \'[]\'::json) from (select {cols_sql} from public."{table}") t'
            ).fetchone()[0]
            (out / f"{table}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1), encoding="utf-8")
            manifest["tables"][table] = {
                "rows": len(rows),
                "columns": kept,
                "omitted_columns": sorted(set(columns) - set(kept)),
            }
            print(f"{table}: {len(rows)} linhas", flush=True)

    (out / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=1), encoding="utf-8")
    print(f"\nBackup salvo em {out}")


if __name__ == "__main__":
    main()
