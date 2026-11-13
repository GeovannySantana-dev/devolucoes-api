# API de integração (somente leitura)

Endereço base: `https://devolucoes.exemplo.com.br/integracao/v1`

Autenticação: cabeçalho `X-API-Key: pdv_...`. A chave é criada pelo gestor no app, em **Menu → Integrações (API)**.
Ela aparece só uma vez. Crie uma chave por sistema: assim dá para revogar só aquela, e ela para de funcionar na hora.
A chave **só lê**: não aceita, recusa, gera NF nem altera nada.

Datas (`inicio`, `fim`, formato `AAAA-MM-DD`) filtram pela **data da solicitação** da devolução, no horário de
Brasília. Sem datas, a consulta usa os últimos 30 dias. Uma consulta cobre no máximo 3 anos.

Filtros comuns:
- `plataforma`: `tiktok`, `shopee`, `mercado_livre` ou `shein`;
- `loja`: o id da loja, que aparece em `/painel` → `stores`.

Valores em reais (BRL).

## `GET /painel`

Tudo o que o painel de Relatórios mostra. `agrupar` aceita `day`, `week` ou `month` e define a série de evolução.

- `kpis.current` e `kpis.previous`: o período escolhido e o anterior de mesmo tamanho, cada um com estes campos:
  - `total`: número de devoluções;
  - `cost`: custo para a empresa;
  - `refunded`: valor reembolsado;
  - `avg_days`: dias para resolver;
  - `inspected`: devoluções conferidas;
  - `defect_rate`: taxa de defeito, de 0 a 1;
  - `mismatch`: veio outro item no pacote.
- `series`: `[{period, total, amount}]`.
- `platforms`: `[{platform, total}]`.
- `reasons`: `[{reason, total, cost}]`.
- `conditions`: `{resale, defect, mismatch, not_inspected}`.
- `products`: top 50 SKUs, cada um `{sku, name, total, units, defects, defect_rate, mismatches, cost}`.
- `stores`: as lojas.

## `GET /custos`

Custo para a empresa e reembolsado. Devoluções canceladas não entram na conta.

O custo tem três partes:
- `shipping`: frete de devolução pago pela loja;
- `product_loss`: peça perdida ou com defeito;
- `operational`: custo operacional.

O valor `total` é a soma das três. `refunded_amount` é a receita devolvida e vem separada do custo.

`agrupar_por` aceita estes valores:

| Valor | Agrupa por |
| --- | --- |
| `month` (padrão) | mês |
| `reason` | motivo |
| `store` | loja |
| `sku` | SKU, com o custo rateado pelos itens |
| `condition` | condição: `resale`, `defect`, `mismatch` ou `unclassified` |
| `liability` | responsável: `buyer`, `seller`, `platform` ou `none` |

Resposta: `{filtros, totals: {returns, shipping, product_loss, operational, total, refunded_amount}, group_by, groups: [...]}`.

## `GET /devolucoes`

Uma linha por devolução, ordenada pela **última atualização**. A resposta vem paginada:
- `pagina`: começa em 1;
- `por_pagina`: 100 por padrão, até 500.

A resposta traz `{pagina, por_pagina, total, tem_mais, devolucoes: [...]}`.

Para sincronizar aos poucos, use `atualizado_desde=2026-10-19T00:00:00-03:00`. Guarde o maior `datas.atualizada` recebido e use-o na próxima consulta.

Campos de cada devolução:
- **Identificação:** `id`, `plataforma`, `plataforma_nome`, `loja{id,nome}`, `pedido` e `id_devolucao_marketplace`.
- **`tipo`:** `customer_return` é a devolução do cliente; `undelivered` é o pacote não entregue que voltou.
- **Situação:** `modalidade`, `status`, `status_marketplace`, `motivo`, `motivo_codigo`, `reembolso` e `decisao`.
- **Conferência e logística:** `condicao` (resale, defect, mismatch ou nulo se não conferida), `logistica` e `rastreio`.
- **Olist:** `nf_devolucao{numero,status}` e `estoque`.
- **`custos`:** `{frete_devolucao, perda_produto, operacional, total, responsavel}`.
- **`datas`:** `{solicitada, prazo, decidida, recebida, resolvida, atualizada}`, em ISO 8601.
- **`itens`:** `[{sku, produto, variacao, quantidade, reembolso}]`.

A resposta não traz dados pessoais do comprador, notas internas nem o conteúdo bruto do marketplace.

## Exemplo

```bash
curl -H "X-API-Key: pdv_SUA_CHAVE" "https://devolucoes.exemplo.com.br/integracao/v1/custos?inicio=2026-10-01&fim=2026-10-31&agrupar_por=sku"
```

Respostas de erro: `401` para chave inválida ou revogada; `422` para filtro inválido, com a explicação em `detail`.
