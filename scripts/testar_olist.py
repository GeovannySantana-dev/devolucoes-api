"""Teste SOMENTE LEITURA do vínculo devolução -> pedido/NF no Olist. Não grava nada.

    .venv\\Scripts\\python scripts\\testar_olist.py [quantidade] [plataforma]
"""

import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy import select  # noqa: E402
from sqlalchemy.orm import selectinload  # noqa: E402

from app.db import new_session  # noqa: E402
from app.erp.olist import OlistError, unwrap_items  # noqa: E402
from app.models import Return  # noqa: E402
from app.services.erp import _ecommerce_numbers, get_olist_client  # noqa: E402


def main() -> None:
    limit = int(sys.argv[1]) if len(sys.argv) > 1 else 8
    platform = sys.argv[2] if len(sys.argv) > 2 else "tiktok"
    client = get_olist_client()
    summary = Counter()

    with new_session() as db:
        rets = db.scalars(
            select(Return)
            .where(Return.platform == platform, Return.return_type != "refund_only")
            .options(selectinload(Return.items))
            .order_by(Return.requested_at.desc())
            .limit(limit)
        ).all()
        db.expunge_all()  # garante que nada será gravado

    for ret in rets:
        skus = sorted({i.seller_sku for i in ret.items if i.seller_sku})
        print(f"\nDevolução {ret.external_return_id} | pedido {ret.external_order_id} | SKUs {skus}")
        try:
            summary_order = None
            for number in _ecommerce_numbers(ret):  # nº do pedido e, no Mercado Livre, o pack_id
                if summary_order := client.find_order_by_ecommerce_number(number):
                    break
            if not summary_order:
                print("  ✗ pedido NÃO encontrado no Olist pelo número do marketplace")
                summary["pedido_nao_encontrado"] += 1
                continue
            order = client.get_order(str(summary_order["id"]))
            eco = order.get("ecommerce") or {}
            print(
                f"  ✓ pedido Olist nº {order.get('numero')} (id {order['id']}) | situação {order.get('situacao')} "
                f"| canal {eco.get('nomeEcommerce')} (id integração {eco.get('id')})"
            )
            summary["pedido_ok"] += 1

            nf_id = str(order.get("id_nota_fiscal") or "")
            if not nf_id or nf_id == "0":
                print("  ✗ pedido sem NF de venda")
                summary["sem_nf"] += 1
                continue
            nota = client.get_invoice(nf_id)
            chave = nota.get("chave_acesso") or ""
            print(f"  ✓ NF de venda nº {nota.get('numero')} | situação {nota.get('situacao')} | chave {'ok' if len(chave) == 44 else 'AUSENTE'}")
            summary["nf_ok" if len(chave) == 44 else "nf_sem_chave"] += 1

            codigos = {str(i.get("codigo")) for i in unwrap_items(nota)}
            faltando = [s for s in skus if s not in codigos]
            if faltando:
                print(f"  ✗ SKUs que não batem com a NF: {faltando} | códigos na NF: {sorted(codigos)}")
                summary["sku_divergente"] += 1
            else:
                print("  ✓ SKUs batem com os códigos da NF")
                summary["sku_ok"] += 1
        except OlistError as exc:
            print(f"  ✗ erro Olist: {exc}")
            summary["erro"] += 1

    print("\nResumo:", dict(summary))


if __name__ == "__main__":
    main()
