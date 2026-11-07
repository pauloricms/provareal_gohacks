"""Estado mutável da simulação, em SQLite SEPARADO dos CSVs.

Guarda o que muda durante a semana: títulos (cópia vigente do ERP), lotes, cadastro vigente
dos fornecedores, solicitações de aprovação, acionamentos, contatos, casos, detectores,
lições, verificações agendadas e a fila de eventos futuros do relógio.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from env import db

SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (chave TEXT PRIMARY KEY, valor TEXT);

-- cópia vigente dos títulos ainda não pagos (aberto/agendado) + ciclo de vida
CREATE TABLE IF NOT EXISTS titulos (
    titulo_id TEXT PRIMARY KEY, fornecedor_id TEXT, numero_nf_digitado TEXT, nf_norm TEXT, oc_id TEXT,
    parcela TEXT, valor REAL, vencimento TEXT, forma_pagamento TEXT, linha_digitavel TEXT,
    cnpj_beneficiario_boleto TEXT, origem_lancamento TEXT, lancado_por TEXT, data_lancamento TEXT,
    centro_custo TEXT, historico TEXT,
    status_erp TEXT, lote_remessa TEXT, lote_original TEXT, banco_pagador TEXT, data_pagamento TEXT,
    estado TEXT, bloqueado INTEGER DEFAULT 0, motivo_bloqueio TEXT,
    cadeia_resultado TEXT, cadeia_json TEXT, cadeia_avaliada_em TEXT,
    reavaliar INTEGER DEFAULT 1, motivo_reavaliacao TEXT, atualizado_em TEXT
);
CREATE INDEX IF NOT EXISTS ix_tit_forn ON titulos(fornecedor_id);
CREATE INDEX IF NOT EXISTS ix_tit_nf ON titulos(nf_norm);
CREATE INDEX IF NOT EXISTS ix_tit_lote ON titulos(lote_remessa);

CREATE TABLE IF NOT EXISTS transicoes (
    id INTEGER PRIMARY KEY AUTOINCREMENT, titulo_id TEXT, de TEXT, para TEXT,
    quando TEXT, ator TEXT, motivo TEXT
);

CREATE TABLE IF NOT EXISTS lotes (
    lote_remessa TEXT PRIMARY KEY, banco_pagador TEXT, data_pagamento TEXT, horario_fechamento TEXT,
    status TEXT, assinado_por TEXT, assinado_em TEXT, fechado_em TEXT, qtd_titulos INTEGER, valor_total REAL
);
CREATE TABLE IF NOT EXISTS lote_saida (
    lote_remessa TEXT, titulo_id TEXT, fornecedor_id TEXT, valor REAL, forma_pagamento TEXT,
    destino TEXT, enviado_em TEXT, ocorrencia TEXT, descricao_ocorrencia TEXT, retorno_em TEXT,
    conciliado_em TEXT, situacao_fim TEXT,
    PRIMARY KEY (lote_remessa, titulo_id)
);
CREATE TABLE IF NOT EXISTS extrato_sim (
    id INTEGER PRIMARY KEY AUTOINCREMENT, banco TEXT, data TEXT, tipo TEXT, descricao TEXT,
    documento TEXT, valor REAL
);

CREATE TABLE IF NOT EXISTS fornecedores_vigente (
    fornecedor_id TEXT PRIMARY KEY, razao_social TEXT, nome_fantasia TEXT, cnpj TEXT, cnpj_raiz TEXT,
    categoria TEXT, banco TEXT, agencia TEXT, conta TEXT, chave_pix TEXT, forma_pagamento_padrao TEXT,
    email_cadastrado TEXT, telefone_cadastrado TEXT, contato_financeiro TEXT, responsavel_interno TEXT,
    contrato_id TEXT, status_cadastro TEXT, data_ultima_alteracao TEXT, ultima_alteracao_por TEXT,
    bloqueado INTEGER DEFAULT 0, motivo_bloqueio TEXT
);
CREATE TABLE IF NOT EXISTS alteracoes_sim (
    alteracao_id TEXT PRIMARY KEY, data_hora TEXT, fornecedor_id TEXT, campo TEXT, valor_anterior TEXT,
    valor_novo TEXT, alterado_por TEXT, origem TEXT, confirmacao_registrada TEXT, confirmacao_como TEXT,
    revertida_por TEXT, revertida_em TEXT
);
CREATE TABLE IF NOT EXISTS comunicacoes_sim (
    comunicacao_id TEXT PRIMARY KEY, data_hora TEXT, canal TEXT, remetente TEXT, nome_remetente TEXT,
    destinatario TEXT, assunto TEXT, texto TEXT, anexos TEXT
);

CREATE TABLE IF NOT EXISTS solicitacoes (
    id TEXT PRIMARY KEY, acao TEXT, parametros TEXT, evidencias TEXT, solicitante TEXT,
    aprovador_humano TEXT, requer TEXT, aprovacoes TEXT, status TEXT, criada_em TEXT,
    decidida_em TEXT, comentario TEXT, resultado TEXT, valor REAL
);
CREATE TABLE IF NOT EXISTS acionamentos (
    id TEXT PRIMARY KEY, pessoa_id TEXT, motivo TEXT, urgencia TEXT, titulos TEXT, quando TEXT,
    ator TEXT, caso_id TEXT, resposta TEXT, respondido_em TEXT
);
CREATE TABLE IF NOT EXISTS contatos_fornecedor (
    id TEXT PRIMARY KEY, fornecedor_id TEXT, canal TEXT, destino TEXT, mensagem TEXT, enviado_em TEXT,
    resposta_prevista_em TEXT, resposta TEXT, respondido_em TEXT, status TEXT, ator TEXT
);
CREATE TABLE IF NOT EXISTS incidentes (
    id TEXT PRIMARY KEY, tipo TEXT, causa_provavel TEXT, titulos TEXT, descricao TEXT, quando TEXT, ator TEXT
);
CREATE TABLE IF NOT EXISTS propostas_processo (
    id TEXT PRIMARY KEY, processo TEXT, descricao TEXT, evidencias TEXT, quando TEXT, ator TEXT, status TEXT
);
CREATE TABLE IF NOT EXISTS respostas_colaborador (
    id TEXT PRIMARY KEY, solicitacao_id TEXT, mensagem TEXT, quando TEXT, ator TEXT
);

-- memória e aprendizado do agente (seção 4 do plano)
CREATE TABLE IF NOT EXISTS casos (
    id TEXT PRIMARY KEY, titulo_ids TEXT, fornecedor_id TEXT, hipotese TEXT, evidencias TEXT,
    p_problema REAL, acoes_candidatas TEXT, acao_escolhida TEXT, custo_esperado REAL, status TEXT,
    pessoa_acionada TEXT, desfecho TEXT, criado_em TEXT, atualizado_em TEXT
);
CREATE TABLE IF NOT EXISTS detectores (
    id TEXT PRIMARY KEY, nome TEXT, descricao TEXT, sql TEXT, severidade TEXT, criado_no_tique TEXT,
    caso_origem TEXT, n_alertas INTEGER DEFAULT 0, n_confirmados INTEGER DEFAULT 0,
    n_falsos INTEGER DEFAULT 0, ativo INTEGER DEFAULT 1, motivo_status TEXT
);
CREATE TABLE IF NOT EXISTS detector_alertas (
    id INTEGER PRIMARY KEY AUTOINCREMENT, detector_id TEXT, quando TEXT, chave TEXT, linha TEXT,
    confirmado INTEGER
);
CREATE TABLE IF NOT EXISTS licoes (
    id TEXT PRIMARY KEY, texto TEXT, origem TEXT, quando TEXT
);
CREATE TABLE IF NOT EXISTS agenda_verificacoes (
    id TEXT PRIMARY KEY, alvo TEXT, quando TEXT, motivo TEXT, criada_em TEXT, criada_por TEXT,
    status TEXT, executada_em TEXT
);

-- fila de eventos do relógio (respostas, retornos, e no futuro eventos injetados)
CREATE TABLE IF NOT EXISTS eventos_futuros (
    id INTEGER PRIMARY KEY AUTOINCREMENT, quando TEXT, tipo TEXT, payload TEXT, origem TEXT,
    processado_em TEXT
);
-- E9: leitura das comunicações por fornecedor (LLM leve), em cache pelo conjunto de comunicações lidas
CREATE TABLE IF NOT EXISTS e9_leituras (
    fornecedor_id TEXT, hash TEXT, comunicacoes TEXT, titulos TEXT, resultado TEXT, modelo TEXT, quando TEXT,
    PRIMARY KEY (fornecedor_id, hash)
);
-- seção 1d: saúde das fontes (a cada carga) e da cadeia por elo/fonte (a cada tique)
CREATE TABLE IF NOT EXISTS fontes_saude (
    quando TEXT, fonte TEXT, canonico TEXT, versao INTEGER, ok INTEGER, problemas TEXT
);
CREATE TABLE IF NOT EXISTS saude_cadeia (
    quando TEXT, tipo TEXT, chave TEXT, n INTEGER, ok INTEGER, quebrado INTEGER, inconclusivo INTEGER,
    nao_se_aplica INTEGER, pct_ok REAL
);
CREATE TABLE IF NOT EXISTS uso_ferramentas (
    dia TEXT, ator TEXT, ferramenta TEXT, n INTEGER, PRIMARY KEY (dia, ator, ferramenta)
);
"""

FMT = "%Y-%m-%d %H:%M:%S"


def nf_norm(numero: Any) -> str | None:
    """Só dígitos, sem zeros à esquerda, sem sufixo de série ('-1', '/1')."""
    if numero is None:
        return None
    s = str(numero).strip()
    if not s:
        return None
    s = re.split(r"[-/]", s)[0]
    s = re.sub(r"\D", "", s).lstrip("0")
    return s or None


def cnpj_raiz(cnpj: Any) -> str | None:
    if cnpj is None:
        return None
    d = re.sub(r"\D", "", str(cnpj))
    return d[:8] if len(d) >= 8 else None


class Estado:
    def __init__(self, caminho: Path | str):
        self.caminho = Path(caminho)
        self.caminho.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self.con = sqlite3.connect(str(self.caminho), check_same_thread=False, isolation_level=None)
        self.con.row_factory = sqlite3.Row
        self.con.execute("PRAGMA journal_mode=WAL")
        self.con.create_function("nf_norm", 1, nf_norm, deterministic=True)
        self.con.create_function("cnpj_raiz", 1, cnpj_raiz, deterministic=True)
        self.con.executescript(SCHEMA)

    # ------------------------------------------------------------------ utilitários
    def q(self, sql: str, params: Iterable = ()) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self.con.execute(sql, tuple(params)).fetchall()]

    def q1(self, sql: str, params: Iterable = ()) -> dict | None:
        r = self.q(sql, params)
        return r[0] if r else None

    def x(self, sql: str, params: Iterable = ()) -> int:
        with self._lock:
            cur = self.con.execute(sql, tuple(params))
            return cur.lastrowid

    def inserir(self, tabela: str, linha: dict, substituir: bool = False) -> int:
        cols = list(linha)
        vals = [json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
                for v in linha.values()]
        verbo = "INSERT OR REPLACE" if substituir else "INSERT"
        return self.x(f"{verbo} INTO {tabela} ({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})", vals)

    def atualizar(self, tabela: str, chave: str, valor_chave: Any, campos: dict) -> None:
        sets = ", ".join(f"{c} = ?" for c in campos)
        vals = [json.dumps(v, ensure_ascii=False, default=str) if isinstance(v, (dict, list)) else v
                for v in campos.values()]
        self.x(f"UPDATE {tabela} SET {sets} WHERE {chave} = ?", [*vals, valor_chave])

    def proximo_id(self, prefixo: str, tabela: str) -> str:
        n = self.q1(f"SELECT count(*) AS n FROM {tabela}")["n"]
        return f"{prefixo}{n + 1:04d}"

    def transacao(self):
        return _Transacao(self)

    # ------------------------------------------------------------------ meta / relógio
    def meta(self, chave: str, padrao: str | None = None) -> str | None:
        r = self.q1("SELECT valor FROM meta WHERE chave = ?", [chave])
        return r["valor"] if r else padrao

    def set_meta(self, chave: str, valor: Any) -> None:
        self.x("INSERT OR REPLACE INTO meta (chave, valor) VALUES (?, ?)", [chave, str(valor)])

    @property
    def agora(self) -> datetime:
        return datetime.strptime(self.meta("agora"), FMT)

    @property
    def agora_txt(self) -> str:
        return self.meta("agora")

    # ------------------------------------------------------------------ carga inicial
    def inicializar(self, inicio: datetime) -> None:
        """Copia dos CSVs (via DuckDB somente leitura) o que é mutável durante a simulação."""
        if self.meta("inicializado") == "1":
            return
        con = db.conexao_leitura()
        try:
            tit = con.execute("""
                SELECT titulo_id, fornecedor_id, numero_nf_digitado, oc_id, parcela, valor,
                       CAST(vencimento AS VARCHAR) AS vencimento, forma_pagamento, linha_digitavel,
                       cnpj_beneficiario_boleto, origem_lancamento, lancado_por,
                       strftime(data_lancamento, '%Y-%m-%d %H:%M:%S') AS data_lancamento,
                       centro_custo, historico, status AS status_erp, lote_remessa, banco_pagador
                FROM contas_a_pagar WHERE status IN ('aberto', 'agendado')
            """).df()
            forn = con.execute("SELECT * FROM fornecedores").df()
            lotes = con.execute("""
                SELECT lote_remessa, banco_pagador, CAST(data_pagamento AS VARCHAR) AS data_pagamento,
                       horario_fechamento, qtd_titulos, valor_total
                FROM lotes_remessa WHERE situacao = 'aguardando fechamento'
            """).df()
        finally:
            con.close()

        ts = inicio.strftime(FMT)
        with self.transacao():
            for r in tit.to_dict("records"):
                r = {k: (None if _vazio(v) else v) for k, v in r.items()}
                r["nf_norm"] = nf_norm(r["numero_nf_digitado"])
                r["lote_original"] = r["lote_remessa"]
                r["estado"] = "agendado" if r["lote_remessa"] else "lancado"
                r["reavaliar"] = 1
                r["motivo_reavaliacao"] = "carga inicial"
                r["atualizado_em"] = ts
                self.inserir("titulos", r)
            for r in forn.to_dict("records"):
                r = {k: (None if _vazio(v) else (str(v) if not isinstance(v, str) else v)) for k, v in r.items()}
                self.inserir("fornecedores_vigente", {
                    "fornecedor_id": r["fornecedor_id"], "razao_social": r["razao_social"],
                    "nome_fantasia": r["nome_fantasia"], "cnpj": r["cnpj"], "cnpj_raiz": cnpj_raiz(r["cnpj"]),
                    "categoria": r["categoria"], "banco": r["banco"], "agencia": r["agencia"], "conta": r["conta"],
                    "chave_pix": r["chave_pix"], "forma_pagamento_padrao": r["forma_pagamento_padrao"],
                    "email_cadastrado": r["email_cadastrado"], "telefone_cadastrado": r["telefone_cadastrado"],
                    "contato_financeiro": r["contato_financeiro"], "responsavel_interno": r["responsavel_interno"],
                    "contrato_id": r["contrato_id"], "status_cadastro": r["status_cadastro"],
                    "data_ultima_alteracao": r["data_ultima_alteracao"],
                    "ultima_alteracao_por": r["ultima_alteracao_por"],
                })
            for r in lotes.to_dict("records"):
                r["status"] = "aberto"
                self.inserir("lotes", r)
            self.set_meta("inicio", ts)
            self.set_meta("agora", ts)
            self.set_meta("inicializado", "1")

    # ------------------------------------------------------------------ visões úteis
    def fornecedor(self, fornecedor_id: str) -> dict | None:
        return self.q1("SELECT * FROM fornecedores_vigente WHERE fornecedor_id = ?", [fornecedor_id])

    def titulo(self, titulo_id: str) -> dict | None:
        return self.q1("SELECT * FROM titulos WHERE titulo_id = ?", [titulo_id])

    def lote(self, lote_remessa: str) -> dict | None:
        return self.q1("SELECT * FROM lotes WHERE lote_remessa = ?", [lote_remessa])

    def tabelas_para_agente(self) -> dict[str, str]:
        """Visões do estado que o SQL do agente enxerga (nome -> SELECT no SQLite)."""
        return {
            "titulos_vigente": "SELECT * FROM titulos",
            "agenda_atual": """SELECT t.lote_remessa, l.banco_pagador, l.data_pagamento AS data_prevista,
                                      l.horario_fechamento, t.titulo_id, t.fornecedor_id, t.valor, t.vencimento,
                                      t.forma_pagamento, t.estado, t.bloqueado
                               FROM titulos t JOIN lotes l ON l.lote_remessa = t.lote_remessa
                               WHERE l.status = 'aberto'""",
            "fornecedores_vigente": "SELECT * FROM fornecedores_vigente",
            "alteracoes_cadastro_novas": "SELECT * FROM alteracoes_sim",
            "comunicacoes_novas": "SELECT * FROM comunicacoes_sim",
            "lotes_sim": "SELECT * FROM lotes",
            "lote_saida": "SELECT * FROM lote_saida",
            "extrato_novo": "SELECT * FROM extrato_sim",
            "transicoes": "SELECT * FROM transicoes",
            "casos": "SELECT * FROM casos",
            "detectores": "SELECT id, nome, descricao, severidade, n_alertas, n_confirmados, n_falsos, ativo FROM detectores",
            "solicitacoes": "SELECT * FROM solicitacoes",
            "acionamentos": "SELECT * FROM acionamentos",
            "contatos_fornecedor": "SELECT * FROM contatos_fornecedor",
        }


def _vazio(v: Any) -> bool:
    try:
        import pandas as pd
        return v is None or (not isinstance(v, str) and pd.isna(v))
    except (TypeError, ValueError):
        return False


class _Transacao:
    def __init__(self, est: Estado):
        self.est = est

    def __enter__(self):
        self.est._lock.acquire()
        self.est.con.execute("BEGIN")
        return self.est

    def __exit__(self, tipo, *_):
        try:
            self.est.con.execute("ROLLBACK" if tipo else "COMMIT")
        finally:
            self.est._lock.release()
        return False
