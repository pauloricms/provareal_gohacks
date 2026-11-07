"""Executor (código, SEM LLM): o único que escreve no mundo, e só através do guarda de política.

- Ação acima da alçada/aprovação dupla vira solicitar_aprovacao sozinha e entra na fila do Comunicador.
- Enquanto uma ação forte espera aprovação, o dinheiro não sai: o título é retirado do lote (reversível).
- acionar_pessoa nunca sai direto: vai para a fila do Comunicador (um card por lote).
- Regra do prazo: lote fechando sem caso decidido -> ação segura de menor custo esperado.
"""
from __future__ import annotations

import json
from datetime import timedelta

from agente.custos import comparar
from agente.esquemas import ACOES_FORTES, Proposta
from agente.papeis import comunicador
from env import ciclo_vida

ROTULO = "executor"


def _pessoas_do_requisito(amb, requer: list[str]) -> list[str]:
    pessoas = []
    for r in requer:
        p = amb.guarda.pessoa_do_requisito(r)
        if p not in pessoas:
            pessoas.append(p)
    return pessoas


def _alvos(amb, ferramenta: str, params: dict, proposta: Proposta) -> list[str]:
    """Títulos que a ação afeta. Nunca 'todos os da proposta': num par duplicado, a proposta cita a cópia E o
    legítimo, e segurar o legítimo enquanto se espera aprovação para cancelar a cópia gera atraso indevido."""
    if params.get("titulo_id"):
        return [params["titulo_id"]]
    fid = params.get("fornecedor_id")  # ação no fornecedor (bloquear, reverter cadastro): títulos dele no caso
    if fid:
        return [t for t in proposta.titulos if (amb.est.titulo(t) or {}).get("fornecedor_id") == fid]
    return []


def executar(amb, caso: dict, proposta: Proposta) -> list[dict]:
    resultados = []
    catalogo = amb.catalogo
    for acao in proposta.acao_escolhida:
        f, p = acao.ferramenta, dict(acao.parametros)
        if "evidencias" in catalogo[f]["parametros"] and not p.get("evidencias"):
            p["evidencias"] = proposta.evidencias[:12]
        if f == "acionar_pessoa":
            item = p.get("motivo") or proposta.resumo_para_humano or proposta.hipotese
            comunicador.enfileirar(amb, p.get("pessoa_id"), caso["id"], p.get("titulos") or proposta.titulos, item)
            resultados.append({"ferramenta": f, "ok": True, "fila_comunicador": p.get("pessoa_id")})
            continue
        res = amb.executar("agente", f, p, rotulo=ROTULO)
        if res.get("erro") == "requer_aprovacao":
            sol = amb.executar("agente", "solicitar_aprovacao", {
                "acao": f, "parametros": p, "evidencias": proposta.evidencias[:12],
                "aprovador_humano": ",".join(res.get("requer_aprovacao_de", []))}, rotulo=ROTULO)
            if sol.get("ok"):
                for pessoa in _pessoas_do_requisito(amb, sol["requer"]):
                    comunicador.enfileirar(amb, pessoa, caso["id"], proposta.titulos,
                                           f"aprovar {f} {json.dumps(p, ensure_ascii=False)[:160]} — "
                                           f"{proposta.resumo_para_humano or proposta.hipotese}",
                                           solicitacao_id=sol["solicitacao_id"])
                if f in ACOES_FORTES:  # enquanto espera a pessoa, o dinheiro DO ALVO da ação não sai
                    for tid in _alvos(amb, f, p, proposta):
                        t = amb.est.titulo(tid)
                        if t and t["lote_remessa"]:
                            r2 = amb.executar("agente", "retirar_do_lote", {
                                "titulo_id": tid, "motivo": f"aguardando aprovação {sol['solicitacao_id']}"}, rotulo=ROTULO)
                            resultados.append({"ferramenta": "retirar_do_lote", "titulo_id": tid, **r2})
            resultados.append({"ferramenta": f, "aprovacao": sol})
            continue
        resultados.append({"ferramenta": f, **res})
    if proposta.detector_sugerido:
        d = proposta.detector_sugerido
        r = amb.executar("agente", "salvar_detector", {"nome": d.nome, "descricao": d.descricao, "sql": d.sql,
                                                       "severidade": "alta" if proposta.p_problema >= 0.7 else "media",
                                                       "caso_origem": caso["id"]}, rotulo=ROTULO)
        resultados.append({"ferramenta": "salvar_detector", **r})
    return resultados


def regra_do_prazo(amb) -> list[dict]:
    """Lote que fecha no próximo tique: título não provado e sem caso decidido recebe a ação segura de menor
    custo esperado (retirar do lote, reversível) ou segue, se pagar for o menor custo. Tudo registrado."""
    est = amb.est
    proximo = (est.agora + timedelta(hours=1)).strftime("%Y-%m-%d %H:%M")
    decisoes = []
    for l in est.q("SELECT * FROM lotes WHERE status = 'aberto' AND (data_pagamento || ' ' || horario_fechamento) = ?",
                   [proximo]):
        for t in est.q("SELECT * FROM titulos WHERE lote_remessa = ? AND estado IN ('agendado', 'reagendado')",
                       [l["lote_remessa"]]):
            cj = json.loads(t["cadeia_json"]) if t["cadeia_json"] else {}
            res = cj.get("resultado")
            if res == "PROVADO" and not cj.get("e9_pendente"):
                continue
            caso = est.q1("SELECT * FROM casos WHERE titulo_ids LIKE ? ORDER BY atualizado_em DESC LIMIT 1",
                          [f'%"{t["titulo_id"]}"%'])
            if caso and caso["status"] in ("decidido", "aguardando_aprovacao"):
                continue
            if caso and caso["p_problema"] is not None:
                p, fonte_p = float(caso["p_problema"]), f"proposta do {caso['id']}"
            else:
                p = 0.5 if res == "QUEBRADO" else 0.15 if res == "DUVIDOSO" else 0.03
                fonte_p = f"padrão para cadeia {res}{' com E9 pendente' if cj.get('e9_pendente') else ''}"
            f = est.fornecedor(t["fornecedor_id"]) or {}
            cmp_ = comparar(amb.politicas, t["valor"], f.get("categoria"), p)
            if cmp_["custos"]["deixar_pagar"] > cmp_["custos"]["segurar"]:
                r = amb.executar("agente", "retirar_do_lote", {
                    "titulo_id": t["titulo_id"],
                    "motivo": f"regra do prazo: lote fecha às {l['horario_fechamento']} sem caso decidido; "
                              f"custo esperado de pagar R$ {cmp_['custos']['deixar_pagar']:.2f} > segurar "
                              f"R$ {cmp_['custos']['segurar']:.2f}"}, rotulo="watchdog")
                amb.executar("agente", "agendar_verificacao", {
                    "alvo": t["titulo_id"], "quando": (est.agora + timedelta(hours=2)).strftime("%Y-%m-%d %H:00"),
                    "motivo": "retirado pela regra do prazo: investigar e devolver a um lote se for legítimo"},
                    rotulo="watchdog")
                acao = "retirado"
            else:
                acao = "segue (pagar é o menor custo esperado)"
                r = {"ok": True}
            d = {"titulo_id": t["titulo_id"], "lote": l["lote_remessa"], "cadeia": res, "p_problema": p,
                 "fonte_p": fonte_p, "custos": cmp_["custos"], "acao": acao, "ok": r.get("ok")}
            decisoes.append(d)
            amb.log.registrar(est.agora_txt, "watchdog", "regra_do_prazo", **d)
    return decisoes
