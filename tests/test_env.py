"""Testes da F1 (ambiente). Rodar: python -m pytest -q
Sem IDs dos dados do evento: o que o teste precisa é descoberto por critério (tests/conftest.py)."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from env import boleto, ciclo_vida, db
from env.ambiente import Ambiente
from env.estado import cnpj_raiz, nf_norm
from env.relogio import destino_pagamento
from tests import auxiliares as h


@pytest.fixture()
def amb(tmp_path):
    # testes do ambiente sem a cadeia (mais rápidos); a cadeia tem os próprios testes em test_cadeia.py
    return Ambiente(tmp_path / "run", cadeia=False)


def avancar_ate(amb: Ambiente, quando: str) -> list[dict]:
    eventos = []
    while amb.est.agora_txt < quando:
        eventos += amb.tique()["eventos"]
    return eventos


def fecha_em(amb, lote: str) -> str:
    l = amb.est.lote(lote)
    return f"{l['data_pagamento']} {l['horario_fechamento']}:00"


def qtd_agenda(lote: str) -> int:
    return db.conexao_leitura().execute("SELECT count(*) FROM agenda_remessa WHERE lote_remessa = ?", [lote]).fetchone()[0]


# ---------------------------------------------------------------- dados
def test_normalizacoes():
    assert nf_norm("0001234-1") == "1234"
    assert nf_norm("1234/1") == "1234"
    assert nf_norm("") is None
    assert cnpj_raiz("12.345.678/0001-95") == cnpj_raiz("12.345.678/0002-76") == "12345678"  # CNPJ sintético


def test_litoral_normalizado_e_ids_preservados():
    c = db.conexao_leitura()
    tipos = dict(c.execute("SELECT column_name, data_type FROM information_schema.columns "
                           "WHERE table_name = 'extrato_banco_litoral'").fetchall())
    assert tipos["data"] == "DATE" and tipos["valor"] == "DOUBLE"
    assert c.execute("SELECT count(*) FROM nf_duplicatas WHERE n_dup LIKE '0%'").fetchone()[0] > 0
    assert c.execute("SELECT count(*) FROM contas_bancarias_empresa WHERE agencia LIKE '0%'").fetchone()[0] > 0


@pytest.mark.parametrize("sql", [
    "CREATE TABLE x AS SELECT 1",
    "INSERT INTO fornecedores SELECT * FROM fornecedores LIMIT 1",
    "SELECT * FROM read_csv('_local/qualquer.md')",
    "SET enable_external_access = true",
])
def test_conexao_do_agente_e_somente_leitura(sql):
    c = db.conexao_leitura()
    with pytest.raises(Exception):
        c.execute(sql)


def test_boleto_decodifica_dda():
    c = db.conexao_leitura()
    for linha, valor, venc in c.execute("SELECT linha_digitavel, valor, CAST(vencimento AS VARCHAR) "
                                        "FROM dda_boletos USING SAMPLE 300 ROWS").fetchall():
        d = boleto.decodificar(linha)
        assert d["valida"] and abs(d["valor"] - valor) < 0.005 and d["vencimento"] == venc
    adulterada = "1" + linha[1:] if linha[0] != "1" else "2" + linha[1:]
    assert not boleto.decodificar(adulterada)["valida"]


def test_destino_igual_ao_da_agenda(amb):
    """O destino calculado do cadastro vigente reproduz agenda_remessa.destino_pagamento."""
    c = db.conexao_leitura()
    agenda = dict(c.execute("SELECT titulo_id, destino_pagamento FROM agenda_remessa").fetchall())
    for tid, esperado in agenda.items():
        t = amb.est.titulo(tid)
        assert destino_pagamento(t, amb.est.fornecedor(t["fornecedor_id"])) == esperado, tid


# ---------------------------------------------------------------- relógio, lotes e ciclo de vida
def test_carga_inicial(amb):
    assert amb.est.agora_txt == "2026-11-02 08:00:00"
    lote = h.primeiro_lote(amb)
    assert amb.est.q1("SELECT count(*) n FROM titulos WHERE lote_remessa = ?", [lote])["n"] == qtd_agenda(lote)


def test_lote_sai_no_fechamento_e_retirado_nao_sai(amb):
    mer, lit = h.primeiro_lote(amb, "MER"), h.primeiro_lote(amb, "LIT")
    tid = amb.est.q1("SELECT titulo_id FROM titulos WHERE lote_remessa = ? ORDER BY valor LIMIT 1", [mer])["titulo_id"]
    assert amb.executar("agente", "retirar_do_lote", {"titulo_id": tid, "motivo": "teste"})["ok"]
    ev = [e for e in avancar_ate(amb, fecha_em(amb, mer)) if e["tipo"] == "lote_fechado"]
    assert [e["lote_remessa"] for e in ev] == [mer]  # o 1º dia é feriado: nenhum lote antes
    assert ev[0]["qtd_enviados"] == qtd_agenda(mer) - 1
    assert amb.est.q1("SELECT count(*) n FROM lote_saida WHERE titulo_id = ?", [tid])["n"] == 0
    assert amb.est.titulo(tid)["estado"] == "retido"
    assert amb.est.lote(lit)["status"] == "aberto"
    outro = amb.est.q1("SELECT titulo_id FROM lote_saida LIMIT 1")["titulo_id"]
    r = amb.executar("agente", "agendar_titulo", {"titulo_id": tid, "lote_remessa": mer})
    assert r["erro"] == "lote_fechado"  # depois do fechamento não dá mais para mexer no lote
    assert amb.est.titulo(outro)["estado"] == "enviado"


def test_retorno_concilia_e_baixa(amb):
    mer, lit = h.primeiro_lote(amb, "MER"), h.primeiro_lote(amb, "LIT")
    avancar_ate(amb, "2026-11-03 16:00:00")
    estados = {r["estado"]: r["n"] for r in amb.est.q(
        "SELECT t.estado, count(*) n FROM lote_saida s JOIN titulos t USING (titulo_id) "
        "WHERE s.lote_remessa = ? GROUP BY 1", [mer])}
    assert estados == {"baixado": qtd_agenda(mer)}
    assert amb.est.q1("SELECT count(*) n FROM extrato_sim WHERE banco = 'Banco Meridiano'")["n"] == qtd_agenda(mer)
    tid = amb.est.q1("SELECT titulo_id FROM lote_saida WHERE lote_remessa = ? LIMIT 1", [mer])["titulo_id"]
    assert [x["para"] for x in ciclo_vida.historico(amb.est, tid)] == ["enviado", "retorno_banco", "conciliado", "baixado"]
    t_lit = amb.est.q1("SELECT titulo_id FROM lote_saida WHERE lote_remessa = ? LIMIT 1", [lit])["titulo_id"]
    assert amb.est.titulo(t_lit)["estado"] == "enviado"  # retorno do Litoral só às 18:00


def test_transicao_invalida(amb):
    tid = amb.est.q1("SELECT titulo_id FROM titulos WHERE estado = 'lancado' LIMIT 1")["titulo_id"]
    with pytest.raises(ciclo_vida.TransicaoInvalida):
        ciclo_vida.transicionar(amb.est, tid, "baixado", "teste")


def test_alteracao_de_cadastro_dispara_reavaliacao(amb):
    amb.est.x("UPDATE titulos SET reavaliar = 0")
    fid = amb.est.q1("SELECT fornecedor_id FROM titulos WHERE lote_remessa = ? LIMIT 1",
                     [h.primeiro_lote(amb)])["fornecedor_id"]
    amb.injetar_evento("2026-11-02 09:00:00", "alteracao_cadastro",
                       {"fornecedor_id": fid, "campo": "conta", "valor_novo": "1234567-8", "origem": "email"})
    amb.tique()
    marcados = {r["titulo_id"] for r in amb.est.q("SELECT titulo_id FROM titulos WHERE reavaliar = 1")}
    do_forn = set(ciclo_vida.titulos_afetados(amb.est, fornecedor_id=fid))
    assert marcados == do_forn and marcados
    assert amb.est.fornecedor(fid)["conta"] == "1234567-8"
    assert any(a["valor_novo"] == "1234567-8" for a in amb.alteracoes_cadastro(fid))


def test_fim_do_periodo(amb):
    fim = amb.relogio.fim.strftime("%Y-%m-%d %H:%M")
    dentro = {r["lote_remessa"] for r in amb.est.q("SELECT lote_remessa FROM lotes WHERE "
                                                   "(data_pagamento || ' ' || horario_fechamento) <= ?", [fim])}
    while not amb.relogio.acabou():
        amb.tique()
    assert amb.est.agora == amb.relogio.fim
    st = {r["lote_remessa"]: r["status"] for r in amb.est.q("SELECT lote_remessa, status FROM lotes")}
    assert dentro and all(st[l] == "fechado" for l in dentro)
    assert all(s == "avaliado_no_fim" for l, s in st.items() if l not in dentro)
    assert amb.tique()["eventos"] == []


# ---------------------------------------------------------------- guarda
def _titulo_valor(amb, minimo, maximo):
    return amb.est.q1("SELECT * FROM titulos WHERE lote_remessa = ? AND valor > ? AND valor <= ? LIMIT 1",
                      [h.primeiro_lote(amb), minimo, maximo])


def test_alcada_e_segregacao(amb):
    t = _titulo_valor(amb, 5000, 50000)
    destino = h.segundo_lote(amb)
    amb.executar("agente", "retirar_do_lote", {"titulo_id": t["titulo_id"], "motivo": "teste"})
    args = {"titulo_id": t["titulo_id"], "lote_remessa": destino}
    r = amb.executar("agente", "agendar_titulo", args)
    assert r["erro"] == "requer_aprovacao" and "aprovador" in r["requer_aprovacao_de"]
    s = amb.executar("agente", "solicitar_aprovacao", {"acao": "agendar_titulo", "parametros": args, "evidencias": ["x"]})
    assert s["ok"]
    assert amb.executar("agente", "aprovar_acao", {"solicitacao_id": s["solicitacao_id"]})["erro"] == "papel_negado"
    # aprovadores entram até a solicitação completar (pode haver aprovação dupla, conforme o título)
    for humano in (h.aprovador_comum(amb), h.coordenadora(amb), h.diretora(amb)):
        r = amb.executar(humano, "aprovar_acao", {"solicitacao_id": s["solicitacao_id"]})
        if r.get("status") == "executada":
            break
    assert r["status"] == "executada", r
    assert amb.est.titulo(t["titulo_id"])["lote_remessa"] == destino
    assert amb.est.titulo(t["titulo_id"])["estado"] == "reagendado"


def test_humano_nao_aprova_o_que_solicitou(amb):
    t = _titulo_valor(amb, 5000, 50000)
    apr = h.aprovador_comum(amb)
    amb.executar("agente", "retirar_do_lote", {"titulo_id": t["titulo_id"], "motivo": "teste"})
    amb.atores[apr]["papeis"] |= {"analista_cp"}  # aprovador que também solicita
    s = amb.executar(apr, "solicitar_aprovacao", {
        "acao": "agendar_titulo", "parametros": {"titulo_id": t["titulo_id"], "lote_remessa": h.segundo_lote(amb)},
        "evidencias": ["x"]})
    assert amb.executar(apr, "aprovar_acao", {"solicitacao_id": s["solicitacao_id"]})["erro"] == "segregacao"


def test_aprovacao_dupla_reversao_de_cadastro(amb):
    fid, aid = h.alteracao_de_telefone()
    coord = amb.guarda.pessoa_coord
    args = {"fornecedor_id": fid, "alteracao_id": aid, "evidencias": ["teste"]}
    assert amb.executar("agente", "reverter_alteracao_cadastro", args)["erro"] == "requer_aprovacao"
    s = amb.executar("agente", "solicitar_aprovacao", {"acao": "reverter_alteracao_cadastro", "parametros": args,
                                                       "evidencias": ["teste"]})
    assert s["requer"] == ["aprovador", coord]
    apr = h.aprovador_comum(amb)
    r1 = amb.executar(apr, "aprovar_acao", {"solicitacao_id": s["solicitacao_id"]})
    assert r1["status"] == "pendente" and r1["faltam"] == [coord]
    assert amb.executar(apr, "aprovar_acao", {"solicitacao_id": s["solicitacao_id"]})["erro"] == "segregacao"
    r2 = amb.executar(h.coordenadora(amb), "aprovar_acao", {"solicitacao_id": s["solicitacao_id"]})
    assert r2["status"] == "executada", r2
    alt = next(a for a in amb.alteracoes_cadastro(fid) if a["alteracao_id"] == aid)
    assert amb.est.fornecedor(fid)["telefone_cadastrado"] == alt["valor_anterior"]


def test_acima_de_50mil_so_diretora(amb):
    t = _titulo_valor(amb, 50000, 10**9)
    amb.executar("agente", "retirar_do_lote", {"titulo_id": t["titulo_id"], "motivo": "teste"})
    s = amb.executar("agente", "solicitar_aprovacao", {
        "acao": "agendar_titulo", "parametros": {"titulo_id": t["titulo_id"], "lote_remessa": h.segundo_lote(amb)},
        "evidencias": ["x"]})
    assert amb.guarda.pessoa_diretora in s["requer"]
    assert amb.executar(h.aprovador_comum(amb), "aprovar_acao",
                        {"solicitacao_id": s["solicitacao_id"]})["erro"] == "aprovador_errado"


def test_solicitacao_desnecessaria_e_recusada(amb):
    t = _titulo_valor(amb, 0, 5000)
    r = amb.executar("agente", "solicitar_aprovacao", {"acao": "retirar_do_lote",
                                                       "parametros": {"titulo_id": t["titulo_id"], "motivo": "x"},
                                                       "evidencias": ["x"]})
    assert r["erro"] == "aprovacao_desnecessaria"


def test_contato_so_pelo_canal_do_cadastro_e_resposta_chega(amb):
    fid = h.fornecedor_sem_alteracao(amb)
    r = amb.executar("agente", "contatar_fornecedor", {"fornecedor_id": fid, "canal": "email", "mensagem": "x",
                                                       "destino": "golpista@exemplo.com"})
    assert r["erro"] == "canal_fora_do_cadastro"
    assert amb.executar("agente", "contatar_fornecedor", {"fornecedor_id": fid, "canal": "whatsapp", "mensagem": "x"})["erro"] == "canal_invalido"
    r = amb.executar("agente", "contatar_fornecedor", {"fornecedor_id": fid, "canal": "email", "mensagem": "x"})
    assert r["ok"] and r["destino"] == amb.est.fornecedor(fid)["email_cadastrado"]
    avancar_ate(amb, r["resposta_prevista_em"])
    c = amb.est.q1("SELECT * FROM contatos_fornecedor WHERE id = ?", [r["contato_id"]])
    assert c["status"] == "respondido"
    novas = amb.executar("agente", "listar_comunicacoes", {"remetente": c["destino"]})
    assert any(x["assunto"] == f"Re: contato {r['contato_id']}" for x in novas["comunicacoes"])


def _responder(amb, fid, canal):
    r = amb.executar("agente", "contatar_fornecedor", {"fornecedor_id": fid, "canal": canal, "mensagem": "confirmar"})
    assert r["ok"], r
    enviado = amb.est.agora
    previsto = datetime.fromisoformat(r["resposta_prevista_em"])
    assert previsto - enviado >= timedelta(hours=2) and amb.relogio.horario_comercial(previsto)
    avancar_ate(amb, r["resposta_prevista_em"])
    return amb.est.q1("SELECT * FROM contatos_fornecedor WHERE id = ?", [r["contato_id"]])


def test_mundo_fornecedor_sem_alteracao_confirma_dados(amb):
    fid = h.fornecedor_sem_alteracao(amb)
    f = amb.est.fornecedor(fid)
    c = _responder(amb, fid, "email")
    assert "seguem os mesmos" in c["resposta"] and f["conta"] in c["resposta"]


def test_mundo_canal_alterado_sem_confirmacao_vs_telefone_anterior(amb):
    """Mecanismo do mundo (sem gabarito): troca de telefone e conta sem confirmação."""
    fid = h.fornecedor_sem_alteracao(amb)
    original = amb.est.fornecedor(fid)
    for campo, novo in [("telefone", "(11) 90000-0000"), ("conta", "7777777-7")]:
        amb.injetar_evento("2026-11-02 09:00:00", "alteracao_cadastro",
                           {"fornecedor_id": fid, "campo": campo, "valor_novo": novo, "origem": "email",
                            "confirmacao_registrada": "não"})
    amb.tique()
    pelo_atual = _responder(amb, fid, "telefone")
    assert "7777777-7" in pelo_atual["resposta"] and "solicitada por nós" in pelo_atual["resposta"]
    assert amb.destino_contato(fid, "telefone_anterior") == original["telefone_cadastrado"]
    pelo_anterior = _responder(amb, fid, "telefone_anterior")
    assert "Não solicitamos" in pelo_anterior["resposta"] and original["conta"] in pelo_anterior["resposta"]


def test_limite_diario_de_acionamentos(amb):
    p = amb.guarda.pessoa_coord
    for i in range(40):
        assert amb.executar("agente", "acionar_pessoa", {"pessoa_id": p, "motivo": f"t{i}"})["ok"]
    assert amb.executar("agente", "acionar_pessoa", {"pessoa_id": p, "motivo": "x"})["erro"] == "limite_diario"
    assert amb.executar("agente", "acionar_pessoa", {"pessoa_id": "XX99", "motivo": "x"})["erro"] in ("pessoa_invalida", "limite_diario")


# ---------------------------------------------------------------- ferramentas de investigação
def test_sql_somente_select_e_ve_estado(amb):
    assert amb.executar("agente", "executar_sql", {"sql": "DROP TABLE fornecedores"})["erro"] == "sql_negado"
    assert amb.executar("agente", "executar_sql", {"sql": "SELECT 1; SELECT 2"})["erro"] == "sql_negado"
    r = amb.executar("agente", "executar_sql", {"sql": "SELECT count(*) n FROM agenda_atual a "
                                                       "JOIN contas_a_pagar c USING (titulo_id)"})
    total = db.conexao_leitura().execute("SELECT count(*) FROM agenda_remessa").fetchone()[0]
    assert r["ok"] and r["linhas"][0]["n"] == total


def test_python_isolado(amb):
    r = amb.executar("agente", "executar_python", {"codigo": "print(len(sql('SELECT * FROM fornecedores_vigente')))"})
    total = db.conexao_leitura().execute("SELECT count(*) FROM fornecedores").fetchone()[0]
    assert r["ok"] and r["stdout"].strip() == str(total)
    for cod in ["import os", "open('x')", "print(__builtins__)", "sql(\"select * from read_csv('a')\")",
                "pd.read_csv('C:/x/_local/a.md')", "pd.read_table('x')", "df.to_csv('x')", "import numpy as np; np.load('x')"]:
        assert amb.executar("agente", "executar_python", {"codigo": cod})["erro"] == "python_negado", cod
    r = amb.executar("agente", "executar_python", {"codigo": "print(sql(\"SELECT current_setting('enable_external_access') v\").v[0])"})
    assert r["stdout"].strip() == "False"


def test_detector_roda_e_deduplica(amb):
    r = amb.executar("agente", "salvar_detector", {"nome": "grandes", "descricao": "títulos > 100 mil no lote",
                                                   "sql": "SELECT titulo_id FROM agenda_atual WHERE valor > 100000"})
    assert r["ok"]
    a1 = amb.ferramentas.rodar_detectores()
    assert len(a1) == r["linhas_hoje"] > 0
    assert amb.ferramentas.rodar_detectores() == []
    c = amb.executar("agente", "classificar_alerta", {"alerta_id": a1[0]["alerta_id"], "confirmado": False})
    assert c["falsos"] == 1
    assert amb.executar("agente", "salvar_detector", {"nome": "x", "descricao": "x",
                                                      "sql": "SELECT 1 AS a"})["erro"] == "detector_invalido"


def test_schemas_openai(amb):
    nomes = {s["function"]["name"] for s in amb.ferramentas.schemas_openai({"analista_cp", "auditor"})}
    assert {"executar_sql", "retirar_do_lote", "bloquear_titulo", "acionar_pessoa"} <= nomes
    assert "aprovar_acao" not in nomes and "liberar_lote" not in nomes


def test_pessoas_derivadas_da_politica(amb):
    """Nenhum ID de pessoa fixo no código: coordenadora, diretora, recuperação e tesoureiro saem da política."""
    assert amb.guarda.pessoa_coord in amb.guarda.pessoas
    assert amb.guarda.pessoa_coord != amb.guarda.pessoa_diretora
    assert amb.guarda.pessoa_para("recuperacao") in amb.guarda.pessoas
    assert amb.atores[amb.relogio.tesoureiro]["humano"]
