# Central de Devoluções — Copyright (c) 2026 Geovanny Santana.
# Licenciado sob a PolyForm Noncommercial 1.0.0 (ver LICENSE).
# Uso comercial ou venda exigem autorização por escrito do autor.
"""NF de venda a partir do XML da NF-e, no mesmo formato da nota que o Olist devolve (nota.fiscal.obter).

Usado quando a venda não existe mais no Olist (ex.: limpeza para liberar espaço): os XMLs ficam em arquivos .zip
(OLIST_SALE_XML_ZIPS) e o app monta a nota de devolução a partir deles.
"""

import re
import zipfile
import xml.etree.ElementTree as ET
from functools import lru_cache
from typing import Any

KEY = re.compile(r"(\d{44})")


def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        if "}" in el.tag:
            el.tag = el.tag.split("}", 1)[1]
    return root


def _t(node: ET.Element | None, path: str) -> str:
    if node is None:
        return ""
    found = node.find(path)
    return (found.text or "").strip() if found is not None and found.text else ""


def sale_from_xml(xml: bytes | str) -> dict[str, Any]:
    root = _strip_ns(ET.fromstring(xml))
    inf = root.find(".//infNFe")
    if inf is None:
        raise ValueError("XML sem infNFe")
    ide, emit, dest, tot = inf.find("ide"), inf.find("emit"), inf.find("dest"), inf.find("total/ICMSTot")
    ender = dest.find("enderDest") if dest is not None else None
    doc = _t(dest, "CPF") or _t(dest, "CNPJ")
    itens = []
    for det in inf.findall("det"):
        prod = det.find("prod")
        icms = det.find("imposto/ICMS")
        origem = ""
        if icms is not None and len(icms):
            origem = _t(icms[0], "orig")
        item = {
            "codigo": _t(prod, "cProd"),
            "descricao": _t(prod, "xProd"),
            "unidade": _t(prod, "uCom") or "UN",
            "quantidade": _t(prod, "qCom"),
            "valor_unitario": _t(prod, "vUnCom"),
            "cfop": _t(prod, "CFOP"),
            "ncm": _t(prod, "NCM"),
            "origem": origem,
            "cest": _t(prod, "CEST"),
        }
        ean = _t(prod, "cEAN")
        if ean and ean.upper() != "SEM GTIN":
            item["gtin_ean"] = ean
        itens.append({"item": item})
    key = (inf.get("Id") or "").replace("NFe", "")
    return {
        "numero": _t(ide, "nNF"),
        "serie": _t(ide, "serie"),
        "chave_acesso": key,
        "data_emissao": _t(ide, "dhEmi")[:10],
        "regime_tributario": _t(emit, "CRT"),
        "valor_produtos": _t(tot, "vProd"),
        "valor_desconto": _t(tot, "vDesc") or "0",
        "valor_nota": _t(tot, "vNF"),
        "cliente": {
            "nome": _t(dest, "xNome"),
            "tipo_pessoa": "J" if _t(dest, "CNPJ") else "F",
            "cpf_cnpj": doc,
            "ie": _t(dest, "IE"),
            "endereco": _t(ender, "xLgr"),
            "numero": _t(ender, "nro"),
            "complemento": _t(ender, "xCpl"),
            "bairro": _t(ender, "xBairro"),
            "cep": _t(ender, "CEP"),
            "cidade": _t(ender, "xMun"),
            "uf": _t(ender, "UF"),
            "fone": _t(ender, "fone"),
            "email": _t(dest, "email"),
        },
        "itens": itens,
        "origem_xml": True,
    }


@lru_cache(maxsize=4)
def _index(zips: str) -> dict[str, tuple[str, str]]:
    """chave de acesso -> (arquivo .zip, nome dentro do zip)."""
    index: dict[str, tuple[str, str]] = {}
    for path in (p.strip() for p in zips.split(";") if p.strip()):
        with zipfile.ZipFile(path) as z:
            for name in z.namelist():
                m = KEY.search(name.rsplit("/", 1)[-1])
                if m and name.lower().endswith(".xml"):
                    index[m.group(1)] = (path, name)
    return index


def find_sale_xml(zips: str, key: str | None) -> dict[str, Any] | None:
    if not zips or not key:
        return None
    hit = _index(zips).get(key)
    if not hit:
        return None
    with zipfile.ZipFile(hit[0]) as z:
        return sale_from_xml(z.read(hit[1]))
