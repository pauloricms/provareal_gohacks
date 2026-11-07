"""Custo esperado pelas regras de politicas.csv (nada inventado): decide entre deixar pagar, segurar e escalar."""
from __future__ import annotations

DIAS_ATRASO = 7  # parametros.md: título legítimo que perde o lote atrasa 7 dias corridos


def custo_atraso(pol: dict, valor: float, categoria: str | None, dias: int = DIAS_ATRASO) -> float:
    a = pol["atraso"]
    rel = a["custo_de_relacionamento_por_titulo_atrasado"].get(categoria or "", 0.0)
    return round(valor * float(a["multa"]) + valor * float(a["juros_ao_mes"]) * dias / 30 + float(rel), 2)


def custo_acionamento(pol: dict) -> float:
    return float(pol["acionamento_humano"]["custo_por_acionamento_desnecessario"])


def comparar(pol: dict, valor: float, categoria: str | None, p_problema: float) -> dict:
    """Os três custos da tese: deixar pagar, segurar e escalar. Escolhe o menor."""
    p = min(max(float(p_problema), 0.0), 1.0)
    pagar = round(p * valor, 2)
    segurar = round((1 - p) * custo_atraso(pol, valor, categoria), 2)
    escalar = custo_acionamento(pol)
    opcoes = {"deixar_pagar": pagar, "segurar": segurar, "escalar": escalar}
    return {"p_problema": p, "custos": opcoes, "menor": min(opcoes, key=opcoes.get)}
