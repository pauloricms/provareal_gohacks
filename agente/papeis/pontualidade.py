"""Mesa 2, Pontualidade (código): todo título LEGÍTIMO sai no lote certo, no prazo.

A Integridade tira do lote; a Pontualidade garante que o legítimo VOLTE a tempo. Candidatos:
- título fora de lote cujo caso foi decidido como legítimo (nenhuma ação) ou cuja cadeia voltou a PROVAR;
- título cuja solicitação de ação forte foi RECUSADA pelo humano (a pessoa disse que é legítimo);
- título rejeitado pelo banco cujo destino agora é válido.
O lote-alvo é o primeiro que ainda dá tempo (fecha depois do próximo tique), do mesmo banco pagador, sem antecipar
o vencimento. A ação passa pelo Executor/guarda (acima de R$ 5 mil vira pedido de aprovação, num card do lote).
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from agente import quadro
from agente.custos import custo_atraso
from agente.papeis import comunicador

ROTULO = "pontualidade"
ATIVOS = ("triado", "em_investigacao", "aguardando_aprovacao", "inconclusivo")


def _bloqueado_por_caso(est, tid: str) -> bool:
    for c in est.q(f"SELECT status FROM casos WHERE titulo_ids LIKE ? AND status IN ({','.join('?' * len(ATIVOS))})",
                   [f'%"{tid}"%', *ATIVOS]):
        return True
    return False


def _legitimo(est, t: dict) -> str | None:
    """Motivo para devolver o título ao lote, ou None."""
    tid = t["titulo_id"]
    if _bloqueado_por_caso(est, tid) or t["bloqueado"]:
        return None
    caso = est.q1("SELECT * FROM casos WHERE titulo_ids LIKE ? ORDER BY atualizado_em DESC LIMIT 1", [f'%"{tid}"%'])
    recusa = est.q1("SELECT id, comentario FROM solicitacoes WHERE status = 'recusada' AND parametros LIKE ? "
                    "AND acao IN ('bloquear_titulo','cancelar_titulo','retirar_do_lote','bloquear_fornecedor') "
                    "ORDER BY decidida_em DESC LIMIT 1", [f'%{tid}%'])
    if recusa:
        return f"pessoa recusou segurar ({recusa['id']}: {recusa['comentario']})"
    if caso and caso["status"] == "decidido":
        if json.loads(caso["acao_escolhida"] or "[]") == []:
            return f"caso {caso['id']} decidiu que é legítimo"
        return None  # a Integridade decidiu segurar: só um fato novo (novo caso) muda isso
    cj = json.loads(t["cadeia_json"]) if t["cadeia_json"] else {}
    if cj.get("resultado") == "PROVADO" and not cj.get("e9_pendente"):
        return "cadeia voltou a provar"
    return None


def lote_alvo(amb, t: dict) -> dict | None:
    est = amb.est
    banco = t["banco_pagador"]
    if not banco and t["lote_original"]:
        lo = est.lote(t["lote_original"])
        banco = lo["banco_pagador"] if lo else None
    limite = (est.agora + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    venc = str(t["vencimento"])[:10]
    for l in est.q("SELECT * FROM lotes WHERE status = 'aberto' AND (data_pagamento || ' ' || horario_fechamento) > ? "
                   "ORDER BY data_pagamento, horario_fechamento", [limite]):
        if banco and l["banco_pagador"] != banco:
            continue
        if l["data_pagamento"] < venc:
            continue  # nunca antecipa (antecipação exige aprovação dupla)
        return l
    return None


def rodar(amb) -> list[dict]:
    est = amb.est
    agora = est.agora
    feitos = []
    candidatos = est.q("SELECT * FROM titulos WHERE lote_remessa IS NULL AND bloqueado = 0 "
                       "AND estado IN ('retido', 'cadeia_avaliada', 'aguardando_fornecedor', 'aguardando_aprovacao') "
                       "AND vencimento <= ?", [(agora + timedelta(days=10)).strftime("%Y-%m-%d")])
    for t in candidatos:
        f = est.fornecedor(t["fornecedor_id"]) or {}
        if f.get("bloqueado"):
            continue
        motivo = _legitimo(est, t)
        if not motivo:
            continue
        lote = lote_alvo(amb, t)
        if not lote:
            continue
        risco = custo_atraso(amb.politicas, t["valor"], f.get("categoria"))
        proposta = {"titulo": t["titulo_id"], "risco_atraso": risco, "lote_alvo": lote["lote_remessa"],
                    "acao": "agendar_titulo", "custo_atraso_evitado": risco, "motivo": motivo}
        quadro.nova_proposta(est, caso_id=None, versao=1, agente=ROTULO, modelo="codigo", conteudo=proposta,
                             status="aprovada")
        res = amb.executar("agente", "agendar_titulo", {"titulo_id": t["titulo_id"], "lote_remessa": lote["lote_remessa"]},
                           rotulo=ROTULO)
        if res.get("erro") == "requer_aprovacao":
            pend = est.q1("SELECT id FROM solicitacoes WHERE acao = 'agendar_titulo' AND status = 'pendente' "
                          "AND parametros LIKE ?", [f'%{t["titulo_id"]}%'])
            if pend:
                continue  # já pediu: não pergunta de novo
            sol = amb.executar("agente", "solicitar_aprovacao", {
                "acao": "agendar_titulo", "parametros": {"titulo_id": t["titulo_id"], "lote_remessa": lote["lote_remessa"]},
                "evidencias": [t["titulo_id"]], "aprovador_humano": ",".join(res.get("requer_aprovacao_de", []))},
                rotulo=ROTULO)
            if sol.get("ok"):
                for pessoa in {amb.guarda.pessoa_do_requisito(r) for r in sol["requer"]}:
                    comunicador.enfileirar(amb, pessoa, "PONTUALIDADE", [t["titulo_id"]],
                                           f"devolver ao {lote['lote_remessa']} ({motivo}); atrasar custaria R$ {risco:,.2f}",
                                           solicitacao_id=sol["solicitacao_id"])
            res = {"ok": sol.get("ok"), "aprovacao": sol.get("solicitacao_id")}
        d = {**proposta, "resultado": "agendado" if res.get("ok") and not res.get("aprovacao") else
             f"aguardando aprovação {res.get('aprovacao')}" if res.get("aprovacao") else res.get("erro")}
        amb.log.registrar(est.agora_txt, ROTULO, "proposta_pontualidade", **d)
        feitos.append(d)
    return feitos
