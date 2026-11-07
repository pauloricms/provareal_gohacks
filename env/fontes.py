"""Registro de fontes (seção 1d): cada CSV com nome lógico, adaptador, impressão digital e invariantes.

- A impressão digital esperada (colunas e tipos) fica em env/fontes_contrato.json (versão 1 = adaptadores atuais).
  Só nomes e tipos de colunas: nenhum dado.
- Invariantes são de CONSISTÊNCIA INTERNA (saldo do extrato fecha, duplicatas somam a NF, DV do boleto...),
  nunca regras de fraude.
- verificar(con) roda a cada carga; o Ambiente emite 'drift_fonte' para o que não bater.
- Regra de segurança: dado que o sistema não entende nunca vira PROVADO (os elos que usam a fonte ficam
  INCONCLUSIVOS e o título vai para os agentes).
"""
from __future__ import annotations

import json
from pathlib import Path

import duckdb

from env import boleto, db

CONTRATO = Path(__file__).with_name("fontes_contrato.json")

# fonte -> (nome lógico no modelo canônico, adaptador, elos que dependem dela)
REGISTRO: dict[str, dict] = {
    "contas_a_pagar": {"canonico": "Título", "adaptador": "db._ler_csv", "elos": ["E5", "E7"]},
    "agenda_remessa": {"canonico": "Título (agenda)", "adaptador": "db._ler_csv", "elos": ["E6"]},
    "fornecedores": {"canonico": "Fornecedor", "adaptador": "db._ler_csv", "elos": ["E6"]},
    "alteracoes_cadastro": {"canonico": "Fornecedor (histórico)", "adaptador": "db._ler_csv", "elos": ["E6"]},
    "pedidos_compra": {"canonico": "OC", "adaptador": "db._ler_csv", "elos": ["E1", "E3"]},
    "contratos_fornecedores": {"canonico": "Contrato", "adaptador": "db._ler_csv", "elos": ["E3", "E8"]},
    "notas_fiscais": {"canonico": "NF", "adaptador": "db._ler_csv", "elos": ["E2", "E3"]},
    "nf_duplicatas": {"canonico": "NF (duplicatas)", "adaptador": "db._ler_csv", "elos": ["E2", "E3", "E8"]},
    "recebimentos_mercadoria": {"canonico": "Recebimento", "adaptador": "db._ler_csv", "elos": ["E4"]},
    "dda_boletos": {"canonico": "Boleto registrado", "adaptador": "db._ler_csv", "elos": ["E5", "E6"]},
    "extrato_banco_meridiano": {"canonico": "Movimento bancário", "adaptador": "db._ler_csv", "elos": ["E5"]},
    "extrato_banco_litoral": {"canonico": "Movimento bancário", "adaptador": "db._ler_litoral", "elos": ["E5"]},
    "retorno_remessa": {"canonico": "Retorno", "adaptador": "db._ler_csv", "elos": ["E5"]},
    "usuarios_sistema": {"canonico": "Usuário", "adaptador": "db._ler_csv", "elos": ["E7"]},
    "log_auditoria_erp": {"canonico": "Trilha de auditoria", "adaptador": "db._ler_csv", "elos": ["E7"]},
    "aprovacoes": {"canonico": "Aprovação", "adaptador": "db._ler_csv", "elos": ["E7"]},
    "feriados_bancarios": {"canonico": "Calendário", "adaptador": "db._ler_csv", "elos": ["E8"]},
    "comunicacoes_fornecedores": {"canonico": "Comunicação", "adaptador": "db._ler_csv", "elos": ["E9"]},
    "lotes_remessa": {"canonico": "Lote", "adaptador": "db._ler_csv", "elos": []},
    "contas_bancarias_empresa": {"canonico": "Conta pagadora", "adaptador": "db._ler_csv", "elos": []},
    "centros_custo": {"canonico": "Centro de custo", "adaptador": "db._ler_csv", "elos": []},
    "plano_contas": {"canonico": "Plano de contas", "adaptador": "db._ler_csv", "elos": []},
    "politicas": {"canonico": "Política", "adaptador": "db._ler_csv", "elos": []},
    "ferramentas": {"canonico": "Ferramentas", "adaptador": "db._ler_csv", "elos": []},
}

FONTES_POR_ELO: dict[str, list[str]] = {}
for _f, _m in REGISTRO.items():
    for _e in _m["elos"]:
        FONTES_POR_ELO.setdefault(_e, []).append(_f)


def _q(con, sql: str):
    return con.execute(sql).fetchone()[0]


def _saldo_fecha(con, tabela: str) -> tuple[bool, str]:
    n = _q(con, f"""WITH x AS (SELECT valor, saldo, lag(saldo) OVER (ORDER BY rowid) AS ant FROM {tabela})
                    SELECT count(*) FROM x WHERE ant IS NOT NULL AND abs(ant + valor - saldo) > 0.01""")
    return n == 0, f"{n} linhas em que saldo anterior + movimento ≠ saldo"


def _dda_dv(con, _t) -> tuple[bool, str]:
    linhas = con.execute("SELECT linha_digitavel, valor FROM dda_boletos").fetchall()
    ruins = sum(1 for l, v in linhas if not boleto.decodificar(l)["valida"] or abs(boleto.decodificar(l)["valor"] - v) > 0.005)
    return ruins == 0, f"{ruins} de {len(linhas)} boletos com DV inválido ou valor ≠ linha digitável"


INVARIANTES: dict[str, list[tuple[str, callable]]] = {
    "extrato_banco_meridiano": [
        ("data e valor não nulos", lambda c, t: (_q(c, f"SELECT count(*) FROM {t} WHERE data IS NULL OR valor IS NULL") == 0, "nulos")),
        ("saldo fecha linha a linha", _saldo_fecha),
    ],
    "extrato_banco_litoral": [
        ("data e valor não nulos", lambda c, t: (_q(c, f"SELECT count(*) FROM {t} WHERE data IS NULL OR valor IS NULL") == 0, "nulos")),
        ("saldo fecha linha a linha", _saldo_fecha),
    ],
    "dda_boletos": [("DV e valor da linha digitável", _dda_dv)],
    "nf_duplicatas": [
        ("duplicatas somam o valor da NF", lambda c, t: (lambda n: (n == 0, f"{n} NFs cuja soma das duplicatas ≠ valor total"))(
            _q(c, "SELECT count(*) FROM (SELECT n.chave_acesso FROM notas_fiscais n JOIN nf_duplicatas d USING (chave_acesso) "
                  "GROUP BY n.chave_acesso, n.valor_total HAVING abs(sum(d.valor) - n.valor_total) > 0.05)"))),
    ],
    "notas_fiscais": [
        ("chave de 44 dígitos com o CNPJ do emitente", lambda c, t: (lambda n: (n == 0, f"{n} chaves inconsistentes"))(
            _q(c, "SELECT count(*) FROM notas_fiscais WHERE length(chave_acesso) <> 44 "
                  "OR substr(chave_acesso, 7, 14) <> cnpj_emitente"))),
    ],
    "contas_a_pagar": [
        ("titulo_id único e valor positivo", lambda c, t: (lambda n: (n == 0, f"{n} violações"))(
            _q(c, "SELECT (SELECT count(*) - count(DISTINCT titulo_id) FROM contas_a_pagar) "
                  "+ (SELECT count(*) FROM contas_a_pagar WHERE valor IS NULL OR valor <= 0)"))),
    ],
    "fornecedores": [
        ("fornecedor_id único e CNPJ com 14 dígitos", lambda c, t: (lambda n: (n == 0, f"{n} violações"))(
            _q(c, "SELECT (SELECT count(*) - count(DISTINCT fornecedor_id) FROM fornecedores) + (SELECT count(*) "
                  "FROM fornecedores WHERE length(regexp_replace(cnpj, '\\D', '', 'g')) <> 14)"))),
    ],
}


def impressao_digital(con, tabela: str) -> dict:
    cols = con.execute(f"DESCRIBE {tabela}").fetchall()
    return {"colunas": [c[0] for c in cols], "tipos": {c[0]: c[1] for c in cols}}


def gerar_contrato(con=None) -> dict:
    """Grava a versão 1 do contrato a partir da carga atual (só nomes e tipos de coluna)."""
    con = con or db.conexao_leitura()
    contrato = {"versao": 1, "fontes": {t: impressao_digital(con, t) for t in REGISTRO}}
    CONTRATO.write_text(json.dumps(contrato, ensure_ascii=False, indent=1), encoding="utf-8")
    return contrato


def verificar(con: duckdb.DuckDBPyConnection, contrato: dict | None = None) -> list[dict]:
    """Confere cada fonte contra o contrato (colunas/tipos) e as invariantes. Devolve uma linha por fonte."""
    if contrato is None:
        contrato = json.loads(CONTRATO.read_text(encoding="utf-8")) if CONTRATO.exists() else gerar_contrato(con)
    out = []
    for fonte, meta in REGISTRO.items():
        problemas = []
        try:
            atual = impressao_digital(con, fonte)
            esperado = contrato["fontes"].get(fonte)
            if esperado:
                faltam = [c for c in esperado["colunas"] if c not in atual["colunas"]]
                novas = [c for c in atual["colunas"] if c not in esperado["colunas"]]
                tipos = [f"{c}: {esperado['tipos'][c]}→{atual['tipos'][c]}" for c in esperado["colunas"]
                         if c in atual["tipos"] and atual["tipos"][c] != esperado["tipos"][c]]
                if faltam:
                    problemas.append(f"colunas ausentes: {faltam}")
                if novas:
                    problemas.append(f"colunas novas: {novas}")
                if tipos:
                    problemas.append(f"tipos mudaram: {tipos}")
            if _q(con, f"SELECT count(*) FROM {fonte}") == 0:
                problemas.append("fonte vazia")
            for nome, fn in INVARIANTES.get(fonte, []):
                try:
                    ok, det = fn(con, fonte)
                except Exception as e:
                    ok, det = False, f"invariante não pôde rodar: {type(e).__name__}: {e}"[:200]
                if not ok:
                    problemas.append(f"invariante '{nome}': {det}")
        except Exception as e:
            problemas.append(f"fonte ilegível: {type(e).__name__}: {e}"[:200])
        out.append({"fonte": fonte, "canonico": meta["canonico"], "adaptador": meta["adaptador"], "versao": 1,
                    "elos": meta["elos"], "ok": not problemas, "problemas": problemas})
    return out
