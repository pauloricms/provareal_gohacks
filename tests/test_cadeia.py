"""Testes da F2 (cadeia de evidências). Testam o MECANISMO de cada elo com alterações sintéticas aplicadas
a títulos que a própria cadeia prova; não há lista de respostas esperadas (sem gabarito)."""
from __future__ import annotations

import time

import pytest

from env.ambiente import Ambiente
from tests import auxiliares as h

LOTE = h.primeiro_lote_csv("MER")  # primeiro lote a fechar (descoberto nos dados)


@pytest.fixture(scope="module")
def base(tmp_path_factory):
    """Um ambiente só para leitura (sem tique), compartilhado pelos testes que não alteram o estado."""
    return Ambiente(tmp_path_factory.mktemp("cadeia") / "run")


@pytest.fixture()
def amb(tmp_path):
    return Ambiente(tmp_path / "run")


def elo(res: dict, cod: str) -> dict:
    return next(e for e in res["elos"] if e["elo"] == cod)


def provado(amb: Ambiente, forma: str | None = None, so_codigo: bool = False) -> dict:
    """Primeiro título do lote de 03/11 que a cadeia prova (opcionalmente de uma forma de pagamento)."""
    sql = "SELECT titulo_id FROM titulos WHERE lote_remessa = ?" + (" AND forma_pagamento = ?" if forma else "")
    for r in amb.est.q(sql + " ORDER BY titulo_id", [LOTE, forma] if forma else [LOTE]):
        res = amb.cadeia.verificar(r["titulo_id"])
        if res["resultado"] == "PROVADO" and (not so_codigo or not res["e9_pendente"]):
            return res
    pytest.skip("nenhum título provado com esse filtro")


# ---------------------------------------------------------------- relatório do lote
def test_lote_inteiro_rapido_e_com_evidencias(base):
    ids = [r["titulo_id"] for r in base.est.q("SELECT titulo_id FROM titulos WHERE lote_remessa = ?", [LOTE])]
    t0 = time.perf_counter()
    res = [base.cadeia.verificar(i) for i in ids]
    assert time.perf_counter() - t0 < 30
    assert len(res) == len(ids) > 0
    for r in res:
        assert len(r["elos"]) == 9
        st = {e["status"] for e in r["elos"]}  # E9 PENDENTE_LLM não conta; E9 lido entra no resultado
        esperado = "QUEBRADO" if "QUEBRADO" in st else "DUVIDOSO" if "INCONCLUSIVO" in st else "PROVADO"
        assert r["resultado"] == esperado
        for e in r["elos"]:
            assert e["explicacao"]
            if e["status"] in ("QUEBRADO", "INCONCLUSIVO", "PENDENTE_LLM"):
                assert e["evidencias"], (r["titulo_id"], e)
            assert "nan" not in e["explicacao"].lower().split(), e["explicacao"]


def test_ferramenta_verificar_cadeia(base):
    tid = base.est.q1("SELECT titulo_id FROM titulos WHERE lote_remessa = ?", [LOTE])["titulo_id"]
    r = base.executar("agente", "verificar_cadeia", {"titulo_id": tid})
    assert r["ok"] and r["resultado"] in ("PROVADO", "DUVIDOSO", "QUEBRADO") and len(r["elos"]) == 9
    r = base.executar("agente", "verificar_cadeia", {"titulo_id": h.titulo_pago()})  # título já pago (CSV)
    assert r["ok"] and r["pendente"] is False


# ---------------------------------------------------------------- reatividade + certificação
def test_tique_avalia_e_certifica_primeiro_o_lote_que_fecha_antes(amb):
    res = amb.tique()["reavaliacao"]
    assert res["avaliados"] == 400 and res["restantes"] > 0
    tits = amb.est.q("SELECT estado, cadeia_resultado, cadeia_json FROM titulos WHERE lote_remessa = ?", [LOTE])
    assert all(t["cadeia_resultado"] for t in tits)  # o lote das 10h foi todo avaliado no primeiro tique
    import json
    for t in tits:
        r = json.loads(t["cadeia_json"])
        if r["resultado"] == "PROVADO" and not r["e9_pendente"]:
            assert t["estado"] == "certificado"
        else:
            assert t["estado"] == "agendado"


def test_troca_bancaria_sem_confirmacao_quebra_e6_e_descertifica(amb):
    alvo = provado(amb, "ted", so_codigo=True)
    tid, fid = alvo["titulo_id"], alvo["fornecedor_id"]
    amb.tique()
    assert amb.est.titulo(tid)["estado"] == "certificado"
    amb.injetar_evento("2026-11-02 10:00:00", "alteracao_cadastro",
                       {"fornecedor_id": fid, "campo": "conta", "valor_novo": "1111111-1", "origem": "email",
                        "confirmacao_registrada": "não"})
    while amb.est.agora_txt < "2026-11-02 10:00:00":
        amb.tique()
    t = amb.est.titulo(tid)
    assert t["estado"] == "agendado"  # perdeu a certificação sozinho, sem ninguém pedir
    import json
    e6 = elo(json.loads(t["cadeia_json"]), "E6")
    assert e6["status"] == "QUEBRADO" and any(x.startswith("ACS") for x in e6["evidencias"])


@pytest.mark.parametrize("telefone_certo,status", [(True, "OK"), (False, "QUEBRADO")])
def test_confirmacao_so_vale_no_telefone_anterior(amb, telefone_certo, status):
    alvo = provado(amb, "ted")
    fid = alvo["fornecedor_id"]
    tel = amb.est.fornecedor(fid)["telefone_cadastrado"] if telefone_certo else "(99) 99999-0000"
    amb.injetar_evento("2026-11-02 09:00:00", "alteracao_cadastro",
                       {"fornecedor_id": fid, "campo": "conta", "valor_novo": "2222222-2", "origem": "email",
                        "confirmacao_registrada": "sim",
                        "confirmacao_como": f"ligação para {tel} em 02/11/2026 com o financeiro"})
    amb.tique()
    assert elo(amb.cadeia.verificar(alvo["titulo_id"]), "E6")["status"] == status


def test_duplicidade_quebra_e5_nos_dois_titulos(amb):
    alvo = provado(amb)
    t = amb.est.titulo(alvo["titulo_id"])
    copia = {k: t[k] for k in ("fornecedor_id", "numero_nf_digitado", "oc_id", "parcela", "valor", "vencimento",
                               "forma_pagamento", "linha_digitavel", "cnpj_beneficiario_boleto")}
    amb.injetar_evento("2026-11-02 09:00:00", "novo_titulo",
                       {**copia, "titulo_id": "TPSIM01", "lancado_por": h.analista(amb), "origem_lancamento": "manual",
                        "lote_remessa": LOTE})
    amb.tique()
    for a, b in ((alvo["titulo_id"], "TPSIM01"), ("TPSIM01", alvo["titulo_id"])):
        e5 = elo(amb.cadeia.verificar(a), "E5")
        assert e5["status"] == "QUEBRADO" and b in e5["evidencias"]
    # duplicidade NÃO é fracionamento (mesma parcela)
    assert "fracionamento" not in elo(amb.cadeia.verificar("TPSIM01"), "E7")["explicacao"]


def test_fracionamento_quebra_e7(amb):
    fid = amb.est.q1("SELECT fornecedor_id FROM titulos WHERE lote_remessa = ? LIMIT 1", [LOTE])["fornecedor_id"]
    for i, parcela in enumerate(("1/2", "2/2"), start=1):
        amb.injetar_evento("2026-11-02 09:00:00", "novo_titulo", {
            "titulo_id": f"TPSIM1{i}", "fornecedor_id": fid, "numero_nf_digitado": "880088", "parcela": parcela,
            "valor": 3000.0, "vencimento": amb.est.lote(LOTE)["data_pagamento"], "forma_pagamento": "ted", "lancado_por": h.analista(amb),
            "origem_lancamento": "manual", "lote_remessa": LOTE})
    amb.tique()
    e7 = elo(amb.cadeia.verificar("TPSIM11"), "E7")
    assert e7["status"] == "QUEBRADO" and "fracionamento" in e7["explicacao"] and "TPSIM12" in e7["evidencias"]
    assert elo(amb.cadeia.verificar("TPSIM11"), "E2")["status"] == "QUEBRADO"  # NF inexistente
    assert elo(amb.cadeia.verificar("TPSIM11"), "E7")["evidencias"]


def test_valor_do_titulo_diferente_do_boleto_quebra_e3(amb):
    alvo = provado(amb, "boleto")
    amb.est.x("UPDATE titulos SET valor = valor + 100 WHERE titulo_id = ?", [alvo["titulo_id"]])
    e3 = elo(amb.cadeia.verificar(alvo["titulo_id"]), "E3")
    assert e3["status"] == "QUEBRADO" and "boleto cobra" in e3["explicacao"]


def test_antecipacao_quebra_e8(amb):
    alvo = provado(amb)
    amb.est.x("UPDATE titulos SET vencimento = '2026-11-20' WHERE titulo_id = ?", [alvo["titulo_id"]])
    e8 = elo(amb.cadeia.verificar(alvo["titulo_id"]), "E8")
    assert e8["status"] == "QUEBRADO" and "antes do vencimento" in e8["explicacao"]


def test_e9_so_separa_comunicacoes_ligadas(amb):
    alvo = provado(amb, so_codigo=True)
    f = amb.est.fornecedor(alvo["fornecedor_id"])
    nf = amb.est.titulo(alvo["titulo_id"])["nf_norm"]
    # cita outra NF: não liga ao título
    amb.injetar_evento("2026-11-02 09:00:00", "nova_comunicacao", {
        "canal": "email", "remetente": f["email_cadastrado"], "assunto": "Boleto NF 123456789",
        "texto": "Segue boleto da NF 123456789."})
    amb.tique()
    assert elo(amb.cadeia.verificar(alvo["titulo_id"]), "E9")["status"] == "NAO_SE_APLICA"
    # sem NF citada (ex.: aviso geral do fornecedor): liga e fica pendente de leitura pelo LLM
    amb.injetar_evento("2026-11-02 10:00:00", "nova_comunicacao", {
        "canal": "email", "remetente": f["email_cadastrado"], "assunto": "Aviso importante",
        "texto": f"Prezados, aviso sobre os próximos pagamentos. NF {nf} incluída."})
    amb.tique()
    e9 = elo(amb.cadeia.verificar(alvo["titulo_id"]), "E9")
    assert e9["status"] == "PENDENTE_LLM" and any(x.startswith("EMS") for x in e9["evidencias"])
