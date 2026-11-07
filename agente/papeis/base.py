"""Infraestrutura comum dos papéis: prompts, laço de ferramentas SOMENTE LEITURA e registro estruturado."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from pydantic import BaseModel, ValidationError

from agente.llm import LLM, ErroLLM, ferramenta_de_schema

PROMPTS = Path(__file__).resolve().parent.parent / "prompts"
PAPEIS_AGENTE = {"analista_cp", "auditor"}
MAX_RESULTADO = 6000

# ferramentas de leitura (o LLM nunca recebe ferramentas de escrita: só o Executor escreve)
LEITURA_INVESTIGADOR = {"verificar_cadeia", "executar_sql", "listar_tabelas", "descrever_tabela", "consultar_titulo",
                        "consultar_fornecedor", "consultar_historico_cadastro", "consultar_nf",
                        "consultar_pedido_compra", "listar_comunicacoes", "consultar_extrato", "buscar_titulos",
                        "listar_agenda_remessa", "executar_python"}
LEITURA_CRITICO = {"verificar_cadeia", "executar_sql", "descrever_tabela", "consultar_titulo", "consultar_fornecedor",
                   "consultar_historico_cadastro", "consultar_nf", "listar_comunicacoes", "buscar_titulos",
                   "consultar_extrato"}
_CATALOGO: str | None = None


def catalogo_tabelas(amb) -> str:
    """Nomes das tabelas (sistemas + estado vivo) para o SQL não errar nome; colunas via descrever_tabela."""
    global _CATALOGO
    if _CATALOGO is None:
        from env import db
        c = db.cursor(amb.con)
        dados = [f"{t}({', '.join(r[0] for r in c.execute(f'DESCRIBE {t}').fetchall()[:14])})"
                 for t in db.tabelas(c) if t not in ("politicas", "politicas_json", "ferramentas")]
        _CATALOGO = ("Tabelas dos sistemas (02/11 08:00): " + "; ".join(dados) +
                     ".\nVisões do estado vivo: " + ", ".join(amb.est.tabelas_para_agente()) +
                     ". Colunas completas: descrever_tabela.")
    return _CATALOGO


def prompt(nome: str, **campos) -> str:
    texto = (PROMPTS / f"{nome}.md").read_text(encoding="utf-8")
    for k, v in campos.items():
        texto = texto.replace("{" + k + "}", str(v))
    return texto


def sistema(amb, papel: str, **campos) -> str:
    pol = json.dumps(amb.politicas, ensure_ascii=False)
    txt = prompt("base", politica=pol) + "\n\n" + prompt(papel, **campos)
    if papel in ("investigador", "critico", "area"):
        txt += "\n\n## Tabelas\n" + catalogo_tabelas(amb)
    return txt


def compacto(v: Any, limite: int = MAX_RESULTADO) -> str:
    s = json.dumps(v, ensure_ascii=False, default=str)
    if len(s) > limite:
        s = s[:limite] + f' ... [cortado: {len(s)} caracteres; refine a consulta]'
    return s


def laco_ferramentas(llm: LLM, amb, *, papel_llm: str, agente: str, sistema_txt: str, usuario_txt: str,
                     leitura: set[str], schema: type[BaseModel], nome_registro: str, descr_registro: str,
                     orcamento: int, extras: dict[str, tuple[dict, Callable[[dict], Any]]] | None = None,
                     contexto: str = "") -> tuple[BaseModel, int]:
    """O LLM investiga com ferramentas de leitura até registrar a saída estruturada (ou o orçamento acabar).

    tool_choice='required' em toda volta: o modelo sempre chama uma ferramenta (leitura ou o registro)."""
    extras = extras or {}
    tools = amb.ferramentas.schemas_openai(PAPEIS_AGENTE, incluir=leitura)
    tools += [spec for spec, _ in extras.values()]
    tools.append(ferramenta_de_schema(nome_registro, descr_registro, schema))
    msgs: list[dict] = [{"role": "system", "content": sistema_txt}, {"role": "user", "content": usuario_txt}]
    chamadas = 0
    voltas_extras = 0
    modelo = llm.modelo_para(papel_llm)  # um modelo do pool por laço inteiro (IDs de tool_call não trocam de modelo)
    while True:
        esgotado = chamadas >= orcamento
        if esgotado:
            if voltas_extras >= 2:
                raise ErroLLM(f"{agente}: não registrou {nome_registro} depois do orçamento")
            voltas_extras += 1
            msgs.append({"role": "user", "content": f"Orçamento de consultas esgotado. Chame AGORA {nome_registro} "
                                                    f"com o que você já sabe."})
        m = llm.chamar(papel_llm, msgs, tools=tools, tool_choice="required", agente=agente, contexto=contexto,
                       modelo=modelo)
        if not m.tool_calls:
            msgs.append({"role": "assistant", "content": m.content or ""})
            msgs.append({"role": "user", "content": f"Use uma ferramenta ou chame {nome_registro}."})
            chamadas += 1
            continue
        msgs.append({"role": "assistant", "content": m.content or "", "tool_calls": [
            {"id": tc.id, "type": "function", "function": {"name": tc.function.name, "arguments": tc.function.arguments}}
            for tc in m.tool_calls]})
        resultado_final = None
        for tc in m.tool_calls:
            nome = tc.function.name
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as e:
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": f"argumentos não são JSON válido: {e}"})
                continue
            if nome == nome_registro:
                try:
                    resultado_final = schema.model_validate(args)
                    msgs.append({"role": "tool", "tool_call_id": tc.id, "content": "registrado"})
                except ValidationError as e:
                    amb.log.registrar(amb.est.agora_txt, agente, "llm_saida_invalida", erro=str(e)[:600])
                    msgs.append({"role": "tool", "tool_call_id": tc.id,
                                 "content": f"validação falhou, corrija e chame de novo: {str(e)[:800]}"})
                continue
            if esgotado:
                msgs.append({"role": "tool", "tool_call_id": tc.id, "content": "orçamento esgotado: registre agora"})
                continue
            chamadas += 1
            if nome in extras:
                res = extras[nome][1](args)
            elif nome in leitura:
                res = amb.executar("agente", nome, args, rotulo=agente)
            else:
                res = {"ok": False, "erro": "ferramenta_nao_permitida",
                       "mensagem": f"{nome} não está disponível para o {agente} (o LLM não escreve no mundo)"}
            msgs.append({"role": "tool", "tool_call_id": tc.id, "content": compacto(res)})
        if resultado_final is not None:
            return resultado_final, chamadas
