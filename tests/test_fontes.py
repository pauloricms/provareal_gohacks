"""Testes da seção 1d: registro de fontes, drift e regra de segurança (dado não entendido nunca vira PROVADO)."""
from __future__ import annotations

import json

import duckdb

from env import db, fontes
from env.ambiente import Ambiente
from tests import auxiliares as h


def test_fontes_atuais_batem_com_o_contrato():
    res = fontes.verificar(db.conexao_leitura())
    assert len(res) == len(fontes.REGISTRO) and all(r["ok"] for r in res), [r for r in res if not r["ok"]]


def test_drift_de_formato_e_de_invariante_sao_detectados():
    """Simula o Litoral mudando de formato (coluna renomeada) e um extrato cujo saldo não fecha."""
    db.construir_cache()
    con = duckdb.connect()
    con.execute(f"ATTACH '{db.CACHE.as_posix()}' AS src (READ_ONLY)")
    for t in fontes.REGISTRO:  # cópia direta (pandas mudaria os tipos e seria, com razão, um drift)
        con.execute(f"CREATE TABLE {t} AS SELECT * FROM src.{t}")
    con.execute("ALTER TABLE extrato_banco_litoral RENAME COLUMN valor TO vlr")
    con.execute("UPDATE extrato_banco_meridiano SET saldo = saldo + 1 WHERE rowid = 10")
    res = {r["fonte"]: r for r in fontes.verificar(con)}
    assert not res["extrato_banco_litoral"]["ok"]
    assert any("colunas ausentes" in p for p in res["extrato_banco_litoral"]["problemas"])
    assert not res["extrato_banco_meridiano"]["ok"]
    assert any("saldo" in p for p in res["extrato_banco_meridiano"]["problemas"])
    assert res["fornecedores"]["ok"]


def test_fonte_com_drift_nunca_prova(tmp_path):
    amb = Ambiente(tmp_path / "run")
    tid = amb.est.q1("SELECT titulo_id FROM titulos WHERE lote_remessa = ?", [h.primeiro_lote(amb)])["titulo_id"]
    assert next(e for e in amb.cadeia.verificar(tid)["elos"] if e["elo"] == "E5")["status"] in ("OK", "QUEBRADO")
    amb.fontes_com_drift = {"extrato_banco_litoral"}
    e5 = next(e for e in amb.cadeia.verificar(tid)["elos"] if e["elo"] == "E5")
    assert e5["status"] != "OK" and ("drift" in e5["explicacao"] or e5["status"] == "QUEBRADO")


def test_saude_da_cadeia_registrada_por_elo_e_fonte(tmp_path):
    amb = Ambiente(tmp_path / "run")
    amb.tique()
    tipos = {r["tipo"] for r in amb.est.q("SELECT DISTINCT tipo FROM saude_cadeia")}
    assert tipos == {"elo", "fonte"}
    e1 = amb.est.q1("SELECT * FROM saude_cadeia WHERE tipo = 'elo' AND chave = 'E1'")
    assert e1["n"] > 0 and 0 <= e1["pct_ok"] <= 100
