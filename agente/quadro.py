"""Quadro compartilhado (seção 2.1): os agentes não conversam livremente; coordenam-se por tabelas no SQLite
de estado. O Chefe escreve TAREFAS, os especialistas escrevem PROPOSTAS, o Crítico escreve VEREDITOS,
o Executor (código) age, o Comunicador escreve CARDS. Travas por título impedem dois casos no mesmo título."""
from __future__ import annotations

import hashlib
import json
from typing import Any

from env.estado import Estado

SCHEMA = """
CREATE TABLE IF NOT EXISTS tarefas (
    id TEXT PRIMARY KEY, caso_id TEXT, mesa TEXT, tipo TEXT, alvo TEXT, prioridade INTEGER, prazo TEXT,
    orcamento INTEGER, status TEXT, criada_em TEXT, concluida_em TEXT, agente TEXT, motivo TEXT, resultado TEXT
);
CREATE TABLE IF NOT EXISTS propostas (
    id TEXT PRIMARY KEY, tarefa_id TEXT, caso_id TEXT, versao INTEGER, agente TEXT, modelo TEXT,
    conteudo TEXT, status TEXT, criada_em TEXT
);
CREATE TABLE IF NOT EXISTS vereditos (
    id TEXT PRIMARY KEY, proposta_id TEXT, caso_id TEXT, agente TEXT, modelo TEXT, reproduziu INTEGER,
    conteudo TEXT, criado_em TEXT
);
CREATE TABLE IF NOT EXISTS travas (titulo_id TEXT PRIMARY KEY, caso_id TEXT, desde TEXT);
CREATE TABLE IF NOT EXISTS fila_comunicador (
    id INTEGER PRIMARY KEY AUTOINCREMENT, pessoa_id TEXT, lote_remessa TEXT, caso_id TEXT, titulos TEXT,
    item TEXT, solicitacao_id TEXT, criada_em TEXT, card_id TEXT
);
CREATE TABLE IF NOT EXISTS cards (
    id TEXT PRIMARY KEY, pessoa_id TEXT, lote_remessa TEXT, itens TEXT, texto TEXT, criado_em TEXT,
    acionamento_id TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS orcamento_tique (quando TEXT PRIMARY KEY, chamadas_llm INTEGER, detalhe TEXT);
"""


def iniciar(est: Estado) -> None:
    est.con.executescript(SCHEMA)
    # coluna extra nos casos (tabela do ambiente): mesa e prazo
    cols = {r["name"] for r in est.q("PRAGMA table_info(casos)")}
    for c, tipo in (("mesa", "TEXT"), ("prazo", "TEXT"), ("valor_em_jogo", "REAL"), ("origem", "TEXT"),
                    ("tentativas", "INTEGER"), ("assinaturas", "TEXT"), ("lote_remessa", "TEXT")):
        if c not in cols:
            est.x(f"ALTER TABLE casos ADD COLUMN {c} {tipo}")


def j(v: Any) -> str:
    return json.dumps(v, ensure_ascii=False, default=str)


def hash_ids(ids: list[str]) -> str:
    return hashlib.sha1("|".join(sorted(ids)).encode()).hexdigest()[:12]


# ---------------------------------------------------------------------- travas
def travar(est: Estado, titulos: list[str], caso_id: str) -> list[str]:
    """Trava os títulos para o caso; devolve os que já estavam com OUTRO caso."""
    ocupados = []
    for t in titulos:
        r = est.q1("SELECT caso_id FROM travas WHERE titulo_id = ?", [t])
        if r and r["caso_id"] != caso_id:
            ocupados.append(t)
        elif not r:
            est.x("INSERT INTO travas VALUES (?, ?, ?)", [t, caso_id, est.agora_txt])
    return ocupados


def destravar(est: Estado, caso_id: str) -> None:
    est.x("DELETE FROM travas WHERE caso_id = ?", [caso_id])


def titulos_travados(est: Estado) -> set[str]:
    return {r["titulo_id"] for r in est.q("SELECT titulo_id FROM travas")}


# ---------------------------------------------------------------------- registros
def nova_tarefa(est: Estado, **campos) -> str:
    tid = est.proximo_id("TAR", "tarefas")
    est.inserir("tarefas", {"id": tid, "status": "aberta", "criada_em": est.agora_txt, **campos})
    return tid


def nova_proposta(est: Estado, **campos) -> str:
    pid = est.proximo_id("PRP", "propostas")
    est.inserir("propostas", {"id": pid, "criada_em": est.agora_txt, **campos})
    return pid


def novo_veredito(est: Estado, **campos) -> str:
    vid = est.proximo_id("VER", "vereditos")
    est.inserir("vereditos", {"id": vid, "criado_em": est.agora_txt, **campos})
    return vid
