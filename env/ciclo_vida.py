"""Máquina de estados por título (seção 1b, item 1) e índice de dependências (item 2).

Caminho feliz: lancado -> cadeia_avaliada -> agendado -> certificado -> enviado -> retorno_banco
              -> conciliado -> baixado
Desvios: retido, bloqueado, aguardando_fornecedor, aguardando_aprovacao, reagendado, cancelado,
         pago_indevidamente -> recuperacao
"""
from __future__ import annotations

from env.estado import Estado, nf_norm

ABERTOS = {"lancado", "cadeia_avaliada", "retido", "aguardando_fornecedor", "aguardando_aprovacao"}
NA_FILA = {"agendado", "certificado", "reagendado"}
DESVIOS_ESPERA = {"retido", "bloqueado", "aguardando_fornecedor", "aguardando_aprovacao"}
FINAIS = {"baixado", "cancelado", "recuperacao"}

_PRE_ENVIO = ABERTOS | NA_FILA | {"bloqueado"}

TRANSICOES: dict[str, set[str]] = {
    "lancado": {"cadeia_avaliada", "agendado", "reagendado"} | DESVIOS_ESPERA | {"cancelado"},
    "cadeia_avaliada": {"agendado", "reagendado", "lancado"} | DESVIOS_ESPERA | {"cancelado"},
    "agendado": {"certificado", "enviado", "reagendado"} | DESVIOS_ESPERA | {"cancelado"},
    "certificado": {"agendado", "enviado", "reagendado"} | DESVIOS_ESPERA | {"cancelado"},
    "reagendado": {"certificado", "enviado", "agendado"} | DESVIOS_ESPERA | {"cancelado"},
    "retido": {"agendado", "reagendado", "lancado", "cadeia_avaliada"} | DESVIOS_ESPERA | {"cancelado"},
    "bloqueado": {"lancado", "retido", "agendado", "reagendado", "cancelado"},
    "aguardando_fornecedor": {"agendado", "reagendado", "lancado", "cadeia_avaliada"} | DESVIOS_ESPERA | {"cancelado"},
    "aguardando_aprovacao": {"agendado", "reagendado", "lancado", "cadeia_avaliada"} | DESVIOS_ESPERA | {"cancelado"},
    "enviado": {"retorno_banco", "pago_indevidamente"},
    "retorno_banco": {"conciliado", "retido", "reagendado", "pago_indevidamente"},
    "conciliado": {"baixado", "pago_indevidamente"},
    "baixado": {"pago_indevidamente"},
    "pago_indevidamente": {"recuperacao"},
    "cancelado": set(),
    "recuperacao": set(),
}
for _o in DESVIOS_ESPERA:
    TRANSICOES[_o].discard(_o)


class TransicaoInvalida(Exception):
    pass


def transicionar(est: Estado, titulo_id: str, para: str, ator: str, motivo: str = "",
                 campos: dict | None = None) -> dict:
    """Muda o estado do título, valida a transição e grava o histórico."""
    t = est.titulo(titulo_id)
    if t is None:
        raise TransicaoInvalida(f"título {titulo_id} não está no estado da simulação")
    de = t["estado"]
    if para not in TRANSICOES:
        raise TransicaoInvalida(f"estado desconhecido: {para}")
    if de == para:
        if campos:
            est.atualizar("titulos", "titulo_id", titulo_id, {**campos, "atualizado_em": est.agora_txt})
        return {"titulo_id": titulo_id, "de": de, "para": para, "mudou": False}
    if para not in TRANSICOES.get(de, set()):
        raise TransicaoInvalida(f"{titulo_id}: transição {de} -> {para} não permitida")
    est.atualizar("titulos", "titulo_id", titulo_id, {**(campos or {}), "estado": para, "atualizado_em": est.agora_txt})
    est.inserir("transicoes", {"titulo_id": titulo_id, "de": de, "para": para, "quando": est.agora_txt,
                               "ator": ator, "motivo": motivo})
    return {"titulo_id": titulo_id, "de": de, "para": para, "mudou": True}


def historico(est: Estado, titulo_id: str) -> list[dict]:
    return est.q("SELECT de, para, quando, ator, motivo FROM transicoes WHERE titulo_id = ? ORDER BY id", [titulo_id])


# ---------------------------------------------------------------------- dependências e reavaliação
def titulos_afetados(est: Estado, *, fornecedor_id: str | None = None, nf: str | None = None,
                     oc_id: str | None = None, titulo_id: str | None = None,
                     apenas_pendentes: bool = True) -> list[str]:
    """Títulos ligados a um fornecedor, NF (normalizada), OC ou título. Só os ainda não pagos."""
    conds, params = [], []
    if fornecedor_id:
        conds.append("fornecedor_id = ?"); params.append(fornecedor_id)
    if nf:
        conds.append("nf_norm = ?"); params.append(nf_norm(nf))
    if oc_id:
        conds.append("oc_id = ?"); params.append(oc_id)
    if titulo_id:
        conds.append("titulo_id = ?"); params.append(titulo_id)
    if not conds:
        return []
    sql = f"SELECT titulo_id FROM titulos WHERE ({' OR '.join(conds)})"
    if apenas_pendentes:
        sql += " AND estado IN (%s)" % ",".join(f"'{s}'" for s in _PRE_ENVIO)
    return [r["titulo_id"] for r in est.q(sql, params)]


def marcar_reavaliacao(est: Estado, titulo_ids: list[str], motivo: str) -> int:
    for tid in titulo_ids:
        est.x("UPDATE titulos SET reavaliar = 1, motivo_reavaliacao = ? WHERE titulo_id = ?", [motivo, tid])
    return len(titulo_ids)


def pendentes_reavaliacao(est: Estado, limite: int | None = None) -> list[dict]:
    # prioridade: lote que FECHA antes (data + horário de fechamento), depois maior valor; sem lote por último
    sql = ("SELECT t.titulo_id, t.motivo_reavaliacao FROM titulos t LEFT JOIN lotes l USING (lote_remessa) "
           "WHERE t.reavaliar = 1 AND t.estado IN (%s) "
           "ORDER BY (l.lote_remessa IS NULL), l.data_pagamento, l.horario_fechamento, t.vencimento, t.valor DESC"
           ) % ",".join(f"'{s}'" for s in _PRE_ENVIO)
    if limite:
        sql += f" LIMIT {int(limite)}"
    return est.q(sql)


def registrar_avaliacao(est: Estado, titulo_id: str, resultado: str, detalhe: dict | list | None) -> None:
    est.atualizar("titulos", "titulo_id", titulo_id, {
        "cadeia_resultado": resultado, "cadeia_json": detalhe, "cadeia_avaliada_em": est.agora_txt,
        "reavaliar": 0,
    })
