# Central de Devoluções — Copyright (c) 2026 Geovanny Santana Bonfim.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""Reaplica as regras de sugestão (revenda/defeito) nas devoluções ainda não conferidas
e recalcula o custo de todas as devoluções.

Nunca altera a condição de devoluções definidas manualmente na conferência.

    .venv\\Scripts\\python scripts\\reclassificar.py
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import or_, select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.db import new_session  # noqa: E402
from app.models import Return  # noqa: E402
from app.services.costs import compute_costs  # noqa: E402
from app.services.erp import apply_suggested_condition, load_rules  # noqa: E402


def main() -> None:
    with new_session() as db:
        rules = load_rules(db)
        rows = db.scalars(
            select(Return).where(or_(Return.condition_source.is_(None), Return.condition_source == "auto"))
        ).all()
        changes = Counter()
        for ret in rows:
            before = ret.item_condition
            apply_suggested_condition(ret, rules)
            if ret.item_condition != before:
                changes[f"{before or 'sem'} -> {ret.item_condition or 'sem'}"] += 1
        all_rows = db.scalars(select(Return).options(selectinload(Return.items))).all()
        for ret in all_rows:
            compute_costs(ret)
        db.commit()
    print(f"{len(rows)} devoluções avaliadas; custo recalculado em {len(all_rows)}")
    for k, v in changes.most_common():
        print(f"  {k}: {v}")


if __name__ == "__main__":
    main()
