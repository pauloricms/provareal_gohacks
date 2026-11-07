"""Agentes de Compras e de Tesouraria: respondem perguntas de outros agentes com FATOS do próprio domínio
(LLM leve + SQL restrito às tabelas da área). Antes de humano, outro agente (regra 2.3.5)."""
from __future__ import annotations

import re

from agente.esquemas import RespostaArea
from agente.llm import LLM
from agente.papeis.base import laco_ferramentas, prompt, sistema

DOMINIOS = {
    "compras": ["pedidos_compra", "recebimentos_mercadoria", "contratos_fornecedores", "centros_custo",
                "notas_fiscais", "nf_duplicatas", "fornecedores_vigente"],
    "tesouraria": ["extrato_banco_meridiano", "extrato_banco_litoral", "extrato_novo", "retorno_remessa",
                   "lotes_remessa", "lotes_sim", "lote_saida", "contas_bancarias_empresa", "titulos_vigente",
                   "contas_a_pagar"],
}
ESPEC_PERGUNTAR = {"type": "function", "function": {
    "name": "perguntar_agente",
    "description": "Pergunta a outro agente do time (compras: OC, recebimento, contrato; tesouraria: extratos, "
                   "pagamentos manuais, retorno, lotes, saldo). Devolve fatos com evidências.",
    "parameters": {"type": "object", "properties": {
        "area": {"type": "string", "enum": sorted(DOMINIOS)}, "pergunta": {"type": "string"}},
        "required": ["area", "pergunta"]}}}


def responder(llm: LLM, amb, area: str, pergunta: str, quem_pergunta: str, orcamento: int = 4) -> dict:
    if area not in DOMINIOS:
        return {"ok": False, "erro": "area_invalida", "mensagem": f"área deve ser uma de {sorted(DOMINIOS)}"}
    tabelas = DOMINIOS[area]

    def sql_restrito(args: dict) -> dict:
        sql = str(args.get("sql", ""))
        usadas = {m.lower() for m in re.findall(r"(?:from|join)\s+([a-zA-Z_][\w.]*)", sql, re.I)}
        fora = sorted(u for u in usadas if u.split(".")[-1] not in tabelas)
        if fora:
            return {"ok": False, "erro": "fora_do_dominio", "mensagem": f"{fora} não são da área {area}: {tabelas}"}
        return amb.executar("agente", "executar_sql", {"sql": sql}, rotulo=f"agente_{area}")

    espec_sql = next(s for s in amb.ferramentas.schemas_openai({"auditor"}, incluir={"executar_sql"}))
    sis = sistema(amb, "area", AREA=area.upper(), tabelas=", ".join(tabelas), orcamento=orcamento)
    usuario = f"Pergunta de {quem_pergunta}: {pergunta}"
    try:
        resp, _ = laco_ferramentas(llm, amb, papel_llm="leve", agente=f"agente_{area}", sistema_txt=sis,
                                   usuario_txt=usuario, leitura=set(), schema=RespostaArea,
                                   nome_registro="registrar_resposta", descr_registro="Registra os fatos encontrados",
                                   orcamento=orcamento, extras={"executar_sql": (espec_sql, sql_restrito)})
    except Exception as e:
        return {"ok": False, "erro": "agente_indisponivel", "mensagem": str(e)[:200]}
    amb.log.registrar(amb.est.agora_txt, f"agente_{area}", "resposta_agente", pergunta=pergunta,
                      quem_pergunta=quem_pergunta, fatos=resp.fatos, evidencias=resp.evidencias)
    return {"ok": True, "area": area, **resp.model_dump()}
