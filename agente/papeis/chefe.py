"""Chefe de turno: planeja (prioridade = R$ em jogo × urgência) e distribui o orçamento de chamadas de LLM.
Só é chamado quando há escolha a fazer (mais de um caso disputando o orçamento)."""
from __future__ import annotations

import json

from agente import quadro
from agente.esquemas import Plano
from agente.llm import LLM, ErroLLM
from agente.papeis.base import compacto, sistema

ORC_PADRAO = 6


def _resumo_caso(amb, c: dict) -> dict:
    f = amb.est.fornecedor(c["fornecedor_id"]) or {}
    quebrados = set()
    for tid in json.loads(c["titulo_ids"]):
        t = amb.est.titulo(tid)
        if t and t["cadeia_json"]:
            cj = json.loads(t["cadeia_json"])
            quebrados |= {f"{e} quebrado" for e in cj.get("quebrados", [])}
            quebrados |= {f"{e} inconclusivo" for e in cj.get("inconclusivos", [])}
    return {"caso_id": c["id"], "fornecedor": f.get("nome_fantasia"), "categoria": f.get("categoria"),
            "titulos": len(json.loads(c["titulo_ids"])), "valor_em_jogo": c["valor_em_jogo"], "prazo": c["prazo"],
            "elos": sorted(quebrados), "origem": c["origem"]}


def _fallback(casos: list[dict], max_paralelo: int) -> list[tuple[dict, int, str]]:
    ordenados = sorted(casos, key=lambda c: (c["prazo"] or "9999", -(c["valor_em_jogo"] or 0)))
    return [(c, ORC_PADRAO, "ordem por prazo e valor (código)") for c in ordenados[:max_paralelo]]


def planejar(llm: LLM, amb, casos: list[dict], orcamento_total: int, max_paralelo: int) -> list[tuple[dict, int, str]]:
    if not casos or orcamento_total <= 0:
        return []
    if len(casos) == 1:
        escolha = [(casos[0], min(ORC_PADRAO, orcamento_total), "único caso pendente")]
    else:
        lotes = amb.est.q("""SELECT l.lote_remessa, l.data_pagamento || ' ' || l.horario_fechamento AS fecha_em,
                                    count(t.titulo_id) n, round(sum(t.valor), 2) valor,
                                    sum(t.estado = 'certificado') certificados
                             FROM lotes l LEFT JOIN titulos t USING (lote_remessa) WHERE l.status = 'aberto'
                             GROUP BY 1, 2 ORDER BY 2 LIMIT 4""")
        licoes = [r["texto"] for r in amb.est.q("SELECT texto FROM licoes ORDER BY quando DESC LIMIT 5")]
        briefing = {"agora": amb.est.agora_txt, "proximos_lotes": lotes,
                    "casos": [_resumo_caso(amb, c) for c in casos[:30]], "licoes": licoes,
                    "orcamento_total_chamadas": orcamento_total, "max_investigacoes_paralelas": max_paralelo}
        try:
            plano = llm.estruturado("decisao", [{"role": "system", "content": sistema(amb, "chefe")},
                                                {"role": "user", "content": f"BRIEFING:\n{compacto(briefing, 12000)}"}],
                                    Plano, "registrar_plano", "Registra o plano do tique", agente="chefe")
            por_id = {c["id"]: c for c in casos}
            escolha, gasto = [], 0
            for t in sorted(plano.tarefas, key=lambda t: t.prioridade):
                c = por_id.get(t.caso_id)
                if not c or any(c is e[0] for e in escolha) or len(escolha) >= max_paralelo:
                    continue
                orc = min(t.orcamento_chamadas, orcamento_total - gasto)
                if orc < 3:
                    break
                escolha.append((c, orc, t.motivo))
                gasto += orc
            if not escolha:
                escolha = _fallback(casos, max_paralelo)
        except ErroLLM as e:
            amb.log.registrar(amb.est.agora_txt, "chefe", "llm_falha", erro=str(e)[:300])
            escolha = _fallback(casos, max_paralelo)
    for c, orc, motivo in escolha:
        tid = quadro.nova_tarefa(amb.est, caso_id=c["id"], mesa="integridade", tipo="investigar", alvo=c["fornecedor_id"],
                                 prioridade=escolha.index((c, orc, motivo)) + 1, prazo=c["prazo"], orcamento=orc,
                                 agente="investigador", motivo=motivo)
        c["_tarefa"] = tid
    amb.log.registrar(amb.est.agora_txt, "chefe", "plano", casos_pendentes=len(casos),
                      tarefas=[{"caso_id": c["id"], "orcamento": o, "motivo": m} for c, o, m in escolha])
    return escolha
