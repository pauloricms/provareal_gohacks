"""Crítico: modelo de OUTRA família; vê só a proposta (não o raciocínio) e tenta refutá-la com consultas próprias."""
from __future__ import annotations

from agente.esquemas import Proposta, Veredito
from agente.llm import LLM
from agente.papeis.base import LEITURA_CRITICO, compacto, laco_ferramentas, sistema


def criticar(llm: LLM, amb, caso_id: str, proposta: Proposta, orcamento: int = 4) -> Veredito:
    vista = {"titulos": proposta.titulos, "hipotese": proposta.hipotese, "evidencias": proposta.evidencias,
             "p_problema": proposta.p_problema, "acao_escolhida": [a.model_dump() for a in proposta.acao_escolhida]}
    usuario = f"Agora: {amb.est.agora_txt}. Caso {caso_id}.\nPROPOSTA a verificar:\n{compacto(vista, 6000)}"
    ver, n = laco_ferramentas(
        llm, amb, papel_llm="critico", agente="critico", sistema_txt=sistema(amb, "critico", orcamento=orcamento),
        usuario_txt=usuario, leitura=LEITURA_CRITICO, schema=Veredito, nome_registro="registrar_veredito",
        descr_registro="Registra o veredito sobre a proposta", orcamento=orcamento, contexto=caso_id)
    amb.log.registrar(amb.est.agora_txt, "critico", "veredito", caso_id=caso_id, reproduziu=ver.reproduziu,
                      objecoes=ver.objecoes, evidencias_proprias=ver.evidencias_proprias, chamadas_ferramenta=n)
    return ver
