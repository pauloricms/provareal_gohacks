"""Contratos (schemas fixos) das saídas dos agentes (seção 2.2). Validados com Pydantic."""
from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, Field, field_validator

ACOES_EXECUTOR = {
    "retirar_do_lote", "bloquear_titulo", "bloquear_fornecedor", "cancelar_titulo", "corrigir_lancamento",
    "reverter_alteracao_cadastro", "contatar_fornecedor", "acionar_pessoa", "agendar_verificacao", "abrir_incidente",
    "agendar_titulo", "propor_correcao_processo",
}
# ações "fortes": seguram dinheiro, mexem em cadastro/lançamento ou chamam gente (enquanto esperam aprovação,
# o Executor tira o título do lote)
ACOES_FORTES = {"retirar_do_lote", "bloquear_titulo", "bloquear_fornecedor", "cancelar_titulo", "corrigir_lancamento",
                "reverter_alteracao_cadastro", "acionar_pessoa"}
# regra de custo do Crítico (seção 10): sempre em ação irreversível/cara; pode ser dispensado em reversível e barata
ACOES_IRREVERSIVEIS = {"cancelar_titulo", "reverter_alteracao_cadastro", "corrigir_lancamento", "bloquear_fornecedor",
                       "acionar_pessoa", "propor_correcao_processo"}
ACOES_REVERSIVEIS_BARATAS = {"retirar_do_lote", "contatar_fornecedor", "agendar_verificacao", "abrir_incidente"}


class Acao(BaseModel):
    ferramenta: str = Field(description="uma das ações do Executor")
    parametros: dict[str, Any] = Field(default_factory=dict, description="argumentos exatos da ferramenta")

    @field_validator("ferramenta")
    @classmethod
    def _valida(cls, v: str) -> str:
        if v not in ACOES_EXECUTOR:
            raise ValueError(f"ferramenta '{v}' não é ação do Executor; use uma de {sorted(ACOES_EXECUTOR)}")
        return v


class AcaoCandidata(BaseModel):
    descricao: str
    custo_esperado: float = Field(description="custo esperado em R$ desta alternativa")


class DetectorSugerido(BaseModel):
    nome: str
    descricao: str
    sql: str = Field(description="SELECT que devolve a coluna titulo_id dos títulos com o mesmo padrão")


class Proposta(BaseModel):
    titulos: list[str] = Field(description="títulos do caso")
    hipotese: str
    evidencias: list[str] = Field(description="IDs vistos nas consultas")
    p_problema: float = Field(ge=0, le=1)
    justificativa_p: str = ""
    acoes_candidatas: list[AcaoCandidata] = Field(default_factory=list)
    acao_escolhida: list[Acao] = Field(default_factory=list, description="vazia = título legítimo, segue no lote")
    resumo_para_humano: str = Field("", description="uma ou duas frases para o card, se precisar de pessoa")
    detector_sugerido: DetectorSugerido | None = None


class Veredito(BaseModel):
    reproduziu: bool
    evidencias_proprias: list[str] = Field(default_factory=list)
    objecoes: list[str] = Field(default_factory=list)


class TarefaPlano(BaseModel):
    caso_id: str
    prioridade: int = Field(ge=1)
    orcamento_chamadas: int = Field(ge=1, le=12)
    motivo: str = ""


class Plano(BaseModel):
    tarefas: list[TarefaPlano]
    observacoes: str = ""


class SinalTitulo(BaseModel):
    titulo_id: str
    sinal: Literal["nenhum", "informativo", "contradiz", "ameaca"]
    gravidade: int = Field(ge=0, le=3)
    evidencias: list[str] = Field(default_factory=list)
    resumo: str = ""


class LeituraE9(BaseModel):
    sinais: list[SinalTitulo]


class RespostaArea(BaseModel):
    fatos: list[str]
    evidencias: list[str] = Field(default_factory=list)
