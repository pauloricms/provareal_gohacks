"""Auxiliares que DESCOBREM nos dados o que cada teste precisa (sem IDs dos dados do evento no código público).

Os testes dependem dos CSVs em data/ (que não vão para o repositório); os IDs são escolhidos por critério."""
from __future__ import annotations

from env import db


def lotes(amb, sufixo: str | None = None) -> list[str]:
    """Lotes abertos em ordem de fechamento (sufixo 'MER' ou 'LIT' filtra o banco)."""
    sql = "SELECT lote_remessa FROM lotes WHERE status = 'aberto'"
    if sufixo:
        sql += f" AND lote_remessa LIKE '%-{sufixo}'"
    return [r["lote_remessa"] for r in amb.est.q(sql + " ORDER BY data_pagamento, horario_fechamento")]


def primeiro_lote_csv(sufixo: str = "MER") -> str:
    """Primeiro lote a fechar, direto dos dados (para constantes de módulo, sem Ambiente)."""
    c = db.conexao_leitura()
    try:
        return c.execute("SELECT lote_remessa FROM lotes_remessa WHERE situacao = 'aguardando fechamento' AND "
                         f"lote_remessa LIKE '%-{sufixo}' ORDER BY data_pagamento LIMIT 1").fetchone()[0]
    finally:
        c.close()


def primeiro_lote(amb, sufixo: str = "MER") -> str:
    return lotes(amb, sufixo)[0]


def segundo_lote(amb, sufixo: str = "MER") -> str:
    return lotes(amb, sufixo)[1]


def humano(amb, *, perfil: str | None = None, pessoa_id: str | None = None, sem_pessoa: bool = False) -> str:
    for login, a in amb.atores.items():
        if not a["humano"] or not a["ativo"]:
            continue
        if perfil and a.get("perfil") != perfil:
            continue
        if pessoa_id and a["pessoa_id"] != pessoa_id:
            continue
        if sem_pessoa and a["pessoa_id"]:
            continue
        return login
    raise LookupError("nenhum humano com esse critério")


def aprovador_comum(amb) -> str:
    """Aprovador sem papel nomeado na política (não supre a coordenadora nem a diretora)."""
    return humano(amb, perfil="aprovador", sem_pessoa=True)


def coordenadora(amb) -> str:
    return humano(amb, pessoa_id=amb.guarda.pessoa_coord)


def diretora(amb) -> str:
    return humano(amb, pessoa_id=amb.guarda.pessoa_diretora)


def analista(amb) -> str:
    return humano(amb, perfil="analista_cp")


def fornecedor_sem_alteracao(amb) -> str:
    c = db.conexao_leitura()
    try:
        return c.execute("SELECT fornecedor_id FROM fornecedores WHERE email_cadastrado IS NOT NULL AND "
                         "fornecedor_id NOT IN (SELECT fornecedor_id FROM alteracoes_cadastro) "
                         "ORDER BY fornecedor_id LIMIT 1").fetchone()[0]
    finally:
        c.close()


def alteracao_de_telefone() -> tuple[str, str]:
    c = db.conexao_leitura()
    try:
        return c.execute("SELECT fornecedor_id, alteracao_id FROM alteracoes_cadastro WHERE campo = 'telefone' "
                         "ORDER BY data_hora LIMIT 1").fetchone()
    finally:
        c.close()


def titulo_pago() -> str:
    c = db.conexao_leitura()
    try:
        return c.execute("SELECT titulo_id FROM contas_a_pagar WHERE status = 'pago' ORDER BY titulo_id LIMIT 1").fetchone()[0]
    finally:
        c.close()
