"""Decodificação da linha digitável (47 dígitos), conforme dicionario_de_dados.md.

Posições 1-3 banco do beneficiário; 4 moeda; 34-37 fator de vencimento (dias desde 22/02/2025 + 1000);
38-47 valor em centavos. Campo livre (5-9, 11-20, 22-31): agência (4), conta com dígito (8),
carteira (2), nosso número (11). 10, 21 e 32: DV módulo 10 de cada campo; 33: DV geral módulo 11.
"""
from __future__ import annotations

import re
from datetime import date, timedelta

BASE_FATOR = date(2025, 2, 22)


def _mod10(numero: str) -> int:
    soma, peso = 0, 2
    for d in reversed(numero):
        p = int(d) * peso
        soma += p // 10 + p % 10
        peso = 1 if peso == 2 else 2
    return (10 - soma % 10) % 10


def _mod11_geral(codigo_sem_dv: str) -> int:
    soma, peso = 0, 2
    for d in reversed(codigo_sem_dv):
        soma += int(d) * peso
        peso = 2 if peso == 9 else peso + 1
    resto = soma % 11
    dv = 11 - resto
    return 1 if dv in (0, 10, 11) else dv


def decodificar(linha: str | None) -> dict:
    """Devolve os campos da linha digitável e se os dígitos verificadores conferem."""
    if not linha:
        return {"valida": False, "erro": "linha digitável vazia"}
    s = re.sub(r"\D", "", str(linha))
    if len(s) != 47:
        return {"valida": False, "erro": f"linha digitável com {len(s)} dígitos (esperado 47)"}
    c1, dv1 = s[0:9], int(s[9])
    c2, dv2 = s[10:20], int(s[20])
    c3, dv3 = s[21:31], int(s[31])
    dv_geral = int(s[32])
    fator = int(s[33:37])
    valor = int(s[37:47]) / 100
    livre = s[4:9] + c2 + c3
    codigo_barras_sem_dv = s[0:4] + s[33:47] + livre
    dvs = {
        "dv_campo1": _mod10(c1) == dv1,
        "dv_campo2": _mod10(c2) == dv2,
        "dv_campo3": _mod10(c3) == dv3,
        "dv_geral": _mod11_geral(codigo_barras_sem_dv) == dv_geral,
    }
    return {
        "valida": all(dvs.values()),
        "verificadores": dvs,
        "banco": s[0:3],
        "moeda": s[3],
        "vencimento": (BASE_FATOR + timedelta(days=fator - 1000)).isoformat() if fator >= 1000 else None,
        "valor": valor,
        "agencia": livre[0:4],
        "conta": livre[4:12],
        "carteira": livre[12:14],
        "nosso_numero": livre[14:25],
    }
