"""Testes da F3 sem rede: um LLM falso, determinístico, chama a ferramenta de registro com conteúdo fixo.
Valida o encanamento (quadro, travas, Crítico, Executor + guarda, regra do prazo, card), não a inteligência."""
from __future__ import annotations

import json
import re
from types import SimpleNamespace

import pytest

from agente.llm import ErroLLM
from agente.orquestrador import Orquestrador
from env.ambiente import Ambiente
from tests import auxiliares as h


class LLMFalso:
    def __init__(self, reproduz=True, acao="retirar_do_lote"):
        self.stats = {"chamadas": 0, "falhas": 0, "segundos": 0.0, "tokens": 0}
        self.reproduz, self.acao = reproduz, acao
        self.papeis = []

    def _args(self, nome: str, texto: str) -> dict:
        tits = list(dict.fromkeys(re.findall(r"TP\d{6}", texto)))
        if nome == "registrar_proposta":
            acoes = [{"ferramenta": self.acao, "parametros": {"titulo_id": tits[0], "motivo": "teste"}}] if self.acao else []
            return {"titulos": tits[:1], "hipotese": "teste", "evidencias": tits[:1], "p_problema": 0.8,
                    "acoes_candidatas": [{"descricao": "segurar", "custo_esperado": 10.0}],
                    "acao_escolhida": acoes, "resumo_para_humano": "teste"}
        if nome == "registrar_veredito":
            return {"reproduziu": self.reproduz, "evidencias_proprias": tits[:1],
                    "objecoes": [] if self.reproduz else ["não reproduzi"]}
        if nome == "registrar_leitura":
            return {"sinais": [{"titulo_id": t, "sinal": "informativo", "gravidade": 0} for t in tits]}
        if nome == "registrar_resposta_fornecedor":
            return {"intencao": "pagamento_nao_identificado", "responder": True, "texto": "resposta de teste",
                    "evidencias": tits[:1]}
        if nome == "registrar_curadoria":
            return {"licoes": ["lição de teste"], "propostas_processo": [
                {"processo": "cadastro", "descricao": "trava de teste", "evidencias": tits[:1]}]}
        if nome == "registrar_plano":
            return {"tarefas": [{"caso_id": c, "prioridade": i + 1, "orcamento_chamadas": 3}
                                for i, c in enumerate(dict.fromkeys(re.findall(r"CASO\d{4}", texto)))]}
        return {"fatos": ["ok"], "evidencias": []}

    def modelo_para(self, papel):
        return f"falso-{papel}"

    def chamar(self, papel, mensagens, tools=None, tool_choice=None, agente="", contexto="", modelo=None):
        self.stats["chamadas"] += 1
        self.papeis.append(papel)
        reg = next(t["function"]["name"] for t in tools if t["function"]["name"].startswith("registrar_"))
        texto = " ".join(str(m.get("content") or "") for m in mensagens)
        tc = SimpleNamespace(id=f"c{self.stats['chamadas']}", function=SimpleNamespace(
            name=reg, arguments=json.dumps(self._args(reg, texto))))
        return SimpleNamespace(tool_calls=[tc], content=None)

    def estruturado(self, papel, mensagens, schema, nome_ferramenta, descricao, agente="", contexto=""):
        m = self.chamar(papel, mensagens, tools=[{"function": {"name": nome_ferramenta}}])
        return schema.model_validate(json.loads(m.tool_calls[0].function.arguments))


@pytest.fixture()
def amb(tmp_path):
    return Ambiente(tmp_path / "run")


def _rodar(orq, n):
    return [orq.tique() for _ in range(n)]


def test_tique_triagem_investiga_critica_executa(amb):
    llm = LLMFalso(acao="bloquear_titulo")  # bloquear sempre passa pelo Crítico (só o aprovador desbloqueia)
    orq = Orquestrador(amb, llm=llm, paralelo=2)
    r = orq.tique()
    assert r["casos_pendentes"] > 0 and r["investigados"] == 2 and len(r["decididos"]) == 2
    assert "critico" in llm.papeis and "leve" in llm.papeis   # E9 lido pelo leve, crítico chamado
    casos = amb.est.q("SELECT * FROM casos WHERE status = 'decidido'")
    for c in casos:
        tid = json.loads(c["acao_escolhida"])[0]["parametros"]["titulo_id"]
        assert amb.est.titulo(tid)["estado"] == "bloqueado"   # Executor agiu pelo guarda
    assert amb.est.q1("SELECT count(*) n FROM travas")["n"] == 0
    assert amb.est.q1("SELECT count(*) n FROM vereditos WHERE reproduziu = 1")["n"] == 2
    assert amb.est.q1("SELECT count(*) n FROM e9_leituras")["n"] > 0


def test_critico_dispensado_em_acao_reversivel_com_prova_do_codigo(amb):
    """Regra de custo do Crítico: retirar do lote um título com elo E1-E8 QUEBRADO pelo código dispensa o Crítico."""
    llm = LLMFalso(acao="retirar_do_lote")
    orq = Orquestrador(amb, llm=llm, paralelo=4)
    orq.tique()
    disp = [e for e in amb.log.ler() if e["tipo"] == "critico_dispensado"]
    chamados = amb.est.q("SELECT caso_id FROM vereditos")
    for e in disp:  # todo caso dispensado retirou título com elo quebrado comprovado pelo código
        c = amb.est.q1("SELECT acao_escolhida FROM casos WHERE id = ?", [e["caso_id"]])
        tid = json.loads(c["acao_escolhida"])[0]["parametros"]["titulo_id"]
        assert orq._quebrado_por_codigo(tid) and amb.est.titulo(tid)["estado"] == "retido"
    for v in chamados:  # e os que passaram pelo Crítico não tinham essa prova
        c = amb.est.q1("SELECT acao_escolhida FROM casos WHERE id = ?", [v["caso_id"]])
        tid = json.loads(c["acao_escolhida"])[0]["parametros"]["titulo_id"]
        assert not orq._quebrado_por_codigo(tid)
    assert disp or chamados


def test_executor_segura_so_o_alvo_enquanto_espera_aprovacao(amb):
    """Par duplicado: cancelar a cópia (> R$ 5 mil, exige aprovação) NÃO pode tirar do lote o título legítimo."""
    from agente.esquemas import Acao, Proposta
    from agente.papeis import executor
    from agente import quadro
    quadro.iniciar(amb.est)
    lote = h.primeiro_lote(amb)
    copia, legitimo = [r["titulo_id"] for r in amb.est.q(
        "SELECT titulo_id FROM titulos WHERE lote_remessa = ? AND valor > 5000 AND valor <= 50000 LIMIT 2", [lote])]
    prop = Proposta(titulos=[copia, legitimo], hipotese="duplicidade", evidencias=[copia, legitimo], p_problema=0.99,
                    acao_escolhida=[Acao(ferramenta="cancelar_titulo",
                                         parametros={"titulo_id": copia, "motivo": "cópia", "evidencias": [legitimo]})])
    res = executor.executar(amb, {"id": "CASO-T"}, prop)
    assert any("aprovacao" in r for r in res)
    assert amb.est.titulo(copia)["lote_remessa"] is None        # a cópia sai do lote enquanto espera
    assert amb.est.titulo(legitimo)["lote_remessa"] == lote     # o legítimo continua e paga no prazo


def test_regra_do_critico_por_tipo_de_acao(amb):
    from agente.esquemas import Acao, Proposta
    orq = Orquestrador(amb, llm=LLMFalso())
    tid = amb.est.q1("SELECT titulo_id FROM titulos WHERE lote_remessa IS NOT NULL LIMIT 1")["titulo_id"]
    def p(f, **par):
        return Proposta(titulos=[tid], hipotese="h", evidencias=[tid], p_problema=0.9,
                        acao_escolhida=[Acao(ferramenta=f, parametros={"titulo_id": tid, **par})])
    assert orq._precisa_critico(p("cancelar_titulo", motivo="x"))[0]          # irreversível
    assert orq._precisa_critico(p("bloquear_titulo", motivo="x"))[0]          # só aprovador desfaz
    assert not orq._precisa_critico(p("agendar_verificacao", alvo=tid, quando="2026-11-03 09:00", motivo="x"))[0]


def test_critico_refuta_duas_vezes_vira_inconclusivo(amb):
    orq = Orquestrador(amb, llm=LLMFalso(reproduz=False, acao="bloquear_titulo"), paralelo=1)
    orq.tique()
    c = amb.est.q1("SELECT * FROM casos WHERE status = 'inconclusivo'")
    assert c and c["tentativas"] == 2
    tid = json.loads(c["acao_escolhida"])[0]["parametros"]["titulo_id"]
    assert amb.est.titulo(tid)["estado"] not in ("retido", "bloqueado")  # refutado: o Executor NÃO agiu
    tipos = [e["tipo"] for e in amb.log.ler()]
    assert tipos.count("critico_refutou") == 2


def test_tique_sem_pendencia_nao_chama_llm(amb):
    llm = LLMFalso()
    orq = Orquestrador(amb, llm=llm, paralelo=4, horizonte_h=0, max_rel=0)  # nada a triar, ler nem responder
    r = orq.tique()
    assert r["llm"]["chamadas"] == 0 and r["casos_pendentes"] == 0


def test_bloqueio_acima_da_alcada_vira_solicitacao_e_card(amb):
    # cancelar acima de R$ 5 mil exige aprovador: Executor pede aprovação, retira do lote e o card sai perto do lote
    llm = LLMFalso(acao="cancelar_titulo")
    orq = Orquestrador(amb, llm=llm, paralelo=4)
    while amb.est.agora_txt < "2026-11-03 08:00:00":
        orq.tique()
    sols = amb.est.q("SELECT * FROM solicitacoes")
    cards = amb.est.q("SELECT * FROM cards")
    if not sols:
        pytest.skip("nenhum caso desta execução exigiu aprovação")
    assert cards, "card deve sair até 2 h antes do lote"
    assert all(c["acionamento_id"] for c in cards)
    assert amb.est.q1("SELECT count(*) n FROM acionamentos")["n"] == len(cards)  # 1 acionamento por card


def test_relacionamento_sem_fato_ligado_pede_dado_e_nao_aciona_pessoa(amb):
    """A6: o LLM quer encaminhar a uma pessoa, mas a mensagem não cita NF/título: pede o dado ao fornecedor."""
    from agente.papeis import relacionamento

    class Encaminha(LLMFalso):
        def _args(self, nome, texto):
            if nome == "registrar_resposta_fornecedor":
                return {"intencao": "recebimento_duplicidade", "responder": False, "texto": "",
                        "encaminhar": next(iter(amb.guarda.pessoas)), "motivo_encaminhar": "teste"}
            return super()._args(nome, texto)

    fid = h.fornecedor_sem_alteracao(amb)
    f = amb.est.fornecedor(fid)
    amb.injetar_evento("2026-11-02 09:00:00", "nova_comunicacao", {
        "canal": "email", "remetente": f["email_cadastrado"], "assunto": "Recebemos em dobro",
        "texto": "Recebemos um valor duas vezes no mês passado."})
    amb.tique()
    from agente import quadro
    quadro.iniciar(amb.est)
    out = relacionamento.rodar(Encaminha(), amb, limite=100)
    cid = amb.est.q1("SELECT comunicacao_id FROM comunicacoes_sim WHERE remetente = ?",
                     [f["email_cadastrado"]])["comunicacao_id"]
    meu = next(r for r in out if r["comunicacao_id"] == cid)
    assert meu["pediu_dado"] and meu["encaminhado"] is None and meu["respondeu"] is True
    assert not amb.est.q("SELECT 1 FROM fila_comunicador WHERE caso_id = ?", [f"REL:{cid}"])
    c = amb.est.q1("SELECT * FROM contatos_fornecedor WHERE fornecedor_id = ?", [fid])
    assert c["destino"] == f["email_cadastrado"] and "nota fiscal" in c["mensagem"]


def test_curador_consolida_detectores_quase_iguais(amb):
    from agente.papeis import curador
    sql = "SELECT titulo_id AS chave FROM titulos_vigente WHERE valor > 1"
    for i, (s, chaves) in enumerate([(sql, "abc"), ("select  titulo_id as chave from titulos_vigente where valor > 1", "ab"),
                                     ("SELECT 1 AS chave", "xyz"), ("SELECT 2 AS chave", "xyzw")], 1):
        amb.est.inserir("detectores", {"id": f"DET{i:04d}", "nome": f"d{i}", "sql": s, "ativo": 1,
                                       "n_confirmados": 1 if i == 2 else 0, "criado_no_tique": amb.est.agora_txt})
        for k in chaves:
            amb.est.inserir("detector_alertas", {"detector_id": f"DET{i:04d}", "quando": amb.est.agora_txt, "chave": k})
    out = {m["detector_id"]: m["em"] for m in curador.consolidar_detectores(amb)}
    # 1 e 2: mesmo SQL (fica o com mais confirmações); 3 e 4: 3 de 4 títulos em comum (75%), fica o mais antigo
    assert out == {"DET0001": "DET0002", "DET0004": "DET0003"}
    ativos = {r["id"] for r in amb.est.q("SELECT id FROM detectores WHERE ativo = 1")}
    assert ativos == {"DET0002", "DET0003"}


def test_prazo_manda_na_fila_de_casos(amb):
    orq = Orquestrador(amb, llm=LLMFalso())
    casos = [{"id": "A", "prazo": "2026-11-03 10:00", "origem": "cadeia", "valor_em_jogo": 1},
             {"id": "B", "prazo": "2026-11-03 14:00", "origem": "cadeia", "valor_em_jogo": 9},
             {"id": "C", "prazo": "2026-11-03 14:00", "origem": "verificacao:VER1", "valor_em_jogo": 1}]
    assert [c["id"] for c in orq._prazo_manda(casos)] == ["A", "C"]
    adi = [e for e in amb.log.ler() if e["tipo"] == "caso_adiado"]
    assert [e["caso_id"] for e in adi] == ["B"]
    orq._prazo_manda(casos)  # não repete o evento para o mesmo caso e prazo
    assert len([e for e in amb.log.ler() if e["tipo"] == "caso_adiado"]) == 1


def test_resumo_placar_por_obrigacao_sem_bug(amb):
    """Até o primeiro lote fechar: o resumo conta atraso por obrigação (nunca a mesma duas vezes) e, com o A8
    corrigido, nenhum título fica fora do lote sem ter sido alvo de uma ação decidida (causa 'bug')."""
    from metricas.resumo import resumir
    orq = Orquestrador(amb, llm=LLMFalso(acao="retirar_do_lote"), paralelo=4, max_rel=0)
    while amb.est.agora_txt < "2026-11-03 10:00:00":
        orq.tique()
    amb.est.con.execute("PRAGMA wal_checkpoint(FULL)")
    r = resumir(amb.run_dir)
    assert r["lotes_fechados"], "o primeiro lote deveria ter fechado"
    for l in r["lotes_fechados"]:
        chaves = [tuple(a["obrigacao"].values()) for a in l["obrigacoes_atrasadas"]]
        assert len(chaves) == len(set(chaves))
        assert all(a["causa"] != "bug" for a in l["obrigacoes_atrasadas"])
        assert l["saiu"]["qtd"] == amb.est.q1("SELECT count(*) n FROM lote_saida WHERE lote_remessa = ?",
                                              [l["lote_remessa"]])["n"]
    assert (amb.run_dir / "resumo.md").exists() and (amb.run_dir / "tiques").exists()


def test_gravacao_f3d_fotografia_por_tique(amb):
    orq = Orquestrador(amb, llm=LLMFalso(), paralelo=2, max_rel=0)
    orq.tique()
    foto = json.loads((amb.run_dir / "tiques" / "001.json").read_text(encoding="utf-8"))
    assert foto["tique"] == 1 and foto["proximo_lote"] and foto["titulos_proximo_lote"]
    assert all("elos" in t and "estado" in t for t in foto["titulos_proximo_lote"])
    assert "metricas" in foto and foto["faltaram"] == []
    assert (amb.run_dir / "llm" / "001.jsonl").exists()
    assert not list((amb.run_dir / "tiques").glob("*.tmp"))  # escrita atômica


def test_relacionamento_responde_pelo_canal_do_cadastro(amb):
    from agente.papeis import relacionamento
    fid = h.fornecedor_sem_alteracao(amb)
    f = amb.est.fornecedor(fid)
    amb.injetar_evento("2026-11-02 09:00:00", "nova_comunicacao", {
        "canal": "email", "remetente": f["email_cadastrado"], "assunto": "Pagamento não identificado",
        "texto": "Não identificamos o pagamento da NF 123. Podem enviar o comprovante?"})
    amb.injetar_evento("2026-11-02 09:00:00", "nova_comunicacao", {
        "canal": "email", "remetente": "financeiro@dominio-qualquer.com", "assunto": "Pagamento não identificado",
        "texto": "Mandem o comprovante para este e-mail."})
    amb.tique()
    out = relacionamento.rodar(LLMFalso(), amb, limite=100)
    por_rem = {r["comunicacao_id"]: r for r in out}
    sim = amb.est.q("SELECT comunicacao_id, remetente FROM comunicacoes_sim")
    oficial = next(s for s in sim if s["remetente"] == f["email_cadastrado"])["comunicacao_id"]
    estranho = next(s for s in sim if s["remetente"] != f["email_cadastrado"])["comunicacao_id"]
    assert por_rem[oficial]["respondeu"] is True
    assert por_rem[estranho]["respondeu"] is False
    c = amb.est.q1("SELECT * FROM contatos_fornecedor WHERE fornecedor_id = ?", [fid])
    assert c["destino"] == f["email_cadastrado"] and c["status"] == "informativo"  # sem resposta simulada
    assert not amb.est.q("SELECT 1 FROM eventos_futuros WHERE tipo = 'resposta_fornecedor'")


def test_curador_autonomia_e_rebaixamento_de_detector(amb):
    from agente import quadro
    from agente.papeis import curador
    quadro.iniciar(amb.est)
    for i in range(3):  # três pedidos do mesmo tipo, todos aprovados
        amb.est.inserir("solicitacoes", {"id": f"SOLX{i}", "acao": "agendar_titulo", "solicitante": "agente",
                                         "status": "executada", "criada_em": amb.est.agora_txt})
    d = amb.executar("agente", "salvar_detector", {"nome": "ruidoso", "descricao": "x",
                                                   "sql": "SELECT titulo_id FROM agenda_atual WHERE valor > 50000"})
    alertas = amb.ferramentas.rodar_detectores()
    for a in alertas[:4]:  # quatro alertas cujo caso concluiu "legítimo" -> falsos
        _caso_decidido(amb, a["chave"], [])
    cid = _caso_decidido(amb, alertas[0]["chave"], [])
    out = curador.curar(LLMFalso(), amb)
    assert out["autonomia"] and out["autonomia"][0]["acao"] == "agendar_titulo"
    assert amb.est.q1("SELECT status FROM propostas_autonomia")["status"] == "aguardando aprovação humana"
    det = amb.est.q1("SELECT * FROM detectores WHERE id = ?", [d["detector_id"]])
    assert det["ativo"] == 0 and "rebaixado" in det["motivo_status"]
    assert out["licoes"] == 1 and out["propostas"] == 1
    assert amb.est.q1("SELECT count(*) n FROM propostas_processo")["n"] == 1


def _caso_decidido(amb, tid, acoes):
    from agente import quadro
    quadro.iniciar(amb.est)
    cid = amb.est.proximo_id("CASO", "casos")
    amb.est.inserir("casos", {"id": cid, "titulo_ids": [tid], "fornecedor_id": amb.est.titulo(tid)["fornecedor_id"],
                              "status": "decidido", "acao_escolhida": acoes, "atualizado_em": amb.est.agora_txt})
    return cid


@pytest.mark.parametrize("faixa,espera_aprovacao", [((0, 5000), False), ((5000, 50000), True)])
def test_pontualidade_devolve_legitimo_ao_lote(amb, faixa, espera_aprovacao):
    from agente.papeis import pontualidade
    t = amb.est.q1("SELECT * FROM titulos WHERE lote_remessa = ? AND valor > ? AND valor <= ? LIMIT 1",
                   [h.primeiro_lote(amb), *faixa])
    amb.executar("agente", "retirar_do_lote", {"titulo_id": t["titulo_id"], "motivo": "investigar"})
    _caso_decidido(amb, t["titulo_id"], [])  # Integridade concluiu: legítimo
    feitos = pontualidade.rodar(amb)
    d = next(x for x in feitos if x["titulo"] == t["titulo_id"])
    assert d["lote_alvo"] == h.primeiro_lote(amb)  # o mesmo lote ainda dá tempo
    if espera_aprovacao:  # acima de R$ 5 mil: o guarda exige aprovador -> pedido + fila do comunicador
        assert d["resultado"].startswith("aguardando aprovação")
        assert amb.est.q1("SELECT count(*) n FROM fila_comunicador")["n"] >= 1
        assert pontualidade.rodar(amb) == [] or all(x["titulo"] != t["titulo_id"] for x in pontualidade.rodar(amb))
    else:
        assert d["resultado"] == "agendado"
        assert amb.est.titulo(t["titulo_id"])["lote_remessa"] == h.primeiro_lote(amb)


def test_pontualidade_respeita_decisao_de_segurar(amb):
    from agente.papeis import pontualidade
    t = amb.est.q1("SELECT * FROM titulos WHERE lote_remessa = ? AND valor <= 5000 LIMIT 1",
                   [h.primeiro_lote(amb)])
    amb.executar("agente", "retirar_do_lote", {"titulo_id": t["titulo_id"], "motivo": "suspeita"})
    amb.est.x("UPDATE titulos SET cadeia_json = ? WHERE titulo_id = ?",
              [json.dumps({"resultado": "PROVADO", "e9_pendente": False}), t["titulo_id"]])
    _caso_decidido(amb, t["titulo_id"], [{"ferramenta": "retirar_do_lote", "parametros": {}}])
    assert all(x["titulo"] != t["titulo_id"] for x in pontualidade.rodar(amb))
    assert amb.est.titulo(t["titulo_id"])["lote_remessa"] is None


def test_regra_do_prazo_retira_quebrado_sem_caso(amb):
    class Falha(LLMFalso):
        def chamar(self, *a, **k):
            raise ErroLLM("proxy fora do ar")
    orq = Orquestrador(amb, llm=Falha(), paralelo=4)
    while amb.est.agora_txt < "2026-11-03 09:00:00":
        orq.tique()
    dec = [e for e in amb.log.ler() if e["tipo"] == "regra_do_prazo"]
    assert dec and any(d["acao"] == "retirado" for d in dec)
    assert all(amb.est.titulo(d["titulo_id"])["estado"] == "retido" for d in dec if d["acao"] == "retirado")
