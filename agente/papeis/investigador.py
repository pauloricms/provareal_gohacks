"""Investigador (mesa de Integridade): LLM de decisão com ferramentas SOMENTE LEITURA; devolve uma Proposta."""
from __future__ import annotations

import json

from agente.custos import comparar, custo_atraso
from agente.esquemas import Proposta
from agente.llm import LLM
from agente.papeis import area
from agente.papeis.base import LEITURA_INVESTIGADOR, compacto, laco_ferramentas, sistema


def contexto_caso(amb, caso: dict) -> dict:
    """O que o Investigador recebe: o caso, os elos que não fecharam e o custo de segurar cada título."""
    titulos = json.loads(caso["titulo_ids"])
    f = amb.est.fornecedor(caso["fornecedor_id"]) or {}
    itens = []
    for tid in titulos:
        t = amb.est.titulo(tid)
        if not t:
            continue
        cj = json.loads(t["cadeia_json"]) if t["cadeia_json"] else {}
        elos = [{"elo": e["elo"], "status": e["status"], "explicacao": e["explicacao"], "evidencias": e["evidencias"][:8]}
                for e in cj.get("elos", []) if e["status"] not in ("OK", "NAO_SE_APLICA")]
        lote = amb.est.lote(t["lote_remessa"]) if t["lote_remessa"] else None
        itens.append({
            "titulo_id": tid, "valor": t["valor"], "vencimento": t["vencimento"], "nf": t["numero_nf_digitado"],
            "parcela": t["parcela"], "forma": t["forma_pagamento"], "estado": t["estado"],
            "lote": t["lote_remessa"], "fecha_em": f"{lote['data_pagamento']} {lote['horario_fechamento']}" if lote else None,
            "cadeia": cj.get("resultado"), "elos_nao_ok": elos,
            "custo_de_segurar_se_legitimo": custo_atraso(amb.politicas, t["valor"], f.get("categoria")),
        })
    return {"caso_id": caso["id"], "fornecedor": {k: f.get(k) for k in (
        "fornecedor_id", "razao_social", "nome_fantasia", "cnpj", "categoria", "banco", "agencia", "conta",
        "chave_pix", "email_cadastrado", "telefone_cadastrado", "responsavel_interno", "bloqueado")},
            "origem": caso.get("origem"), "titulos": itens}


def investigar(llm: LLM, amb, caso: dict, orcamento: int, objecoes: list[str] | None = None,
               proposta_anterior: dict | None = None) -> tuple[Proposta, int]:
    ctx = contexto_caso(amb, caso)
    licoes = [r["texto"] for r in amb.est.q("SELECT texto FROM licoes ORDER BY quando DESC LIMIT 8")]
    partes = [f"Agora: {amb.est.agora_txt}.", f"CASO:\n{compacto(ctx, 9000)}"]
    if licoes:
        partes.append("Lições aprendidas:\n- " + "\n- ".join(licoes))
    if objecoes:
        partes.append("O CRÍTICO refutou a proposta anterior. Responda às objeções com novas consultas e revise a "
                      f"proposta.\nProposta anterior: {compacto(proposta_anterior, 3000)}\nObjeções:\n- "
                      + "\n- ".join(objecoes))
    extras = {"perguntar_agente": (area.ESPEC_PERGUNTAR, lambda a: area.responder(
        llm, amb, a.get("area", ""), a.get("pergunta", ""), quem_pergunta=f"investigador/{caso['id']}"))}
    prop, n = laco_ferramentas(
        llm, amb, papel_llm="decisao", agente="investigador", sistema_txt=sistema(amb, "investigador", orcamento=orcamento),
        usuario_txt="\n\n".join(partes), leitura=LEITURA_INVESTIGADOR, schema=Proposta,
        nome_registro="registrar_proposta", descr_registro="Registra a proposta do caso (hipótese, evidências, "
                                                           "p_problema, ações candidatas e escolhida)",
        orcamento=orcamento, extras=extras, contexto=caso["id"])
    # referência de custo esperado (código, pela política) para a trilha
    tot = sum(i["valor"] for i in ctx["titulos"]) or 0.0
    cat = ctx["fornecedor"].get("categoria")
    prop_ref = comparar(amb.politicas, tot, cat, prop.p_problema)
    amb.log.registrar(amb.est.agora_txt, "investigador", "proposta", caso_id=caso["id"], hipotese=prop.hipotese,
                      p_problema=prop.p_problema, acoes=[a.model_dump() for a in prop.acao_escolhida],
                      evidencias=prop.evidencias, custo_esperado_politica=prop_ref, chamadas_ferramenta=n,
                      revisao=bool(objecoes))
    return prop, n
