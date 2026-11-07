"""O 'mundo' do agente: junta estado, relógio, guarda, ferramentas e log.

    amb = Ambiente.novo()                     # cria runs/<timestamp>/ (estado.sqlite + eventos.jsonl)
    amb.executar("agente", "consultar_titulo", {"titulo_id": "<titulo_id>"})
    resumo = amb.tique()                       # avança 1 h: fecha lotes, aplica eventos, reavalia afetados

Tudo que muda dados ligados a um título (alteração de cadastro, comunicação nova, título novo,
resposta de fornecedor, retorno do banco) marca os títulos afetados para reavaliação (seção 1b, item 2).
"""
from __future__ import annotations

import json
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Callable

from env import ciclo_vida, db, fontes, mundo, pos_lote
from env.cadeia import Cadeia
from env.estado import FMT, Estado, cnpj_raiz, nf_norm
from env.eventos import LogEventos
from env.ferramentas import Ferramentas
from env.guarda import Guarda, carregar_atores
from env.relogio import Relogio

RAIZ = Path(__file__).resolve().parent.parent



REAVALIACOES_POR_TIQUE = 400  # orçamento da cadeia por tique (prioridade: lote que fecha antes, maior valor)


class Ambiente:
    def __init__(self, run_dir: Path | str, cadeia: bool = True):
        self.run_dir = Path(run_dir)
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.politicas = db.politicas()
        self.con = db.conexao_leitura(travar=False)  # uso interno; o SQL do agente usa conexões travadas
        self.est = Estado(self.run_dir / "estado.sqlite")
        self.log = LogEventos(self.run_dir / "eventos.jsonl")
        self.relogio = Relogio(self.est, self.log, self.politicas)
        primeira_vez = self.est.meta("inicializado") != "1"
        self.est.inicializar(self.relogio.inicio)
        self.atores = carregar_atores()
        self.ferramentas = Ferramentas(self)
        self.catalogo = self.ferramentas.catalogo
        self.guarda = Guarda(self)
        # seção 1d: confere cada fonte contra o contrato e as invariantes a cada carga
        self.fontes_com_drift: set[str] = set()
        self.verificar_fontes()
        # cadeia de evidências (F2): avaliador da reavaliação reativa e da ferramenta verificar_cadeia
        self.cadeia = Cadeia(self) if cadeia else None
        self.avaliador: Callable[[str], dict] | None = self.cadeia.verificar if self.cadeia else None
        # comportamento do mundo externo (env/mundo.py); o simulado (F7) pode trocar
        self.responder_fornecedor: Callable[["Ambiente", dict], dict | None] = mundo.resposta_historica
        self._oc_status: dict[str, str] | None = None
        self._alt_csv: list[dict] | None = None
        if primeira_vez:
            self.log.registrar(self.est.agora_txt, "ambiente", "inicio", run_dir=str(self.run_dir),
                               titulos_pendentes=self.est.q1("SELECT count(*) n FROM titulos")["n"],
                               lotes_abertos=self.est.q1("SELECT count(*) n FROM lotes")["n"])

    @classmethod
    def novo(cls, base: Path | str | None = None) -> "Ambiente":
        base = Path(base) if base else RAIZ / "runs"
        nome = time.strftime("%Y%m%d_%H%M%S")
        destino = base / nome
        i = 1
        while destino.exists():
            destino = base / f"{nome}_{i}"
            i += 1
        return cls(destino)

    # ------------------------------------------------------------------ execução de ferramentas
    def executar(self, ator: str, ferramenta: str, args: dict | None = None, autorizacao: str | None = None,
                 rotulo: str | None = None) -> dict:
        """rotulo: qual agente do time chamou (só para a trilha); o guarda decide pelo ator."""
        args = dict(args or {})
        meta = self.catalogo.get(ferramenta)
        if meta is None:
            res = {"ok": False, "erro": "ferramenta_desconhecida", "mensagem": f"{ferramenta} não existe"}
            self.log.registrar(self.est.agora_txt, ator, "ferramenta", ferramenta=ferramenta, args=args, resultado=res)
            return res
        aceitos = set(meta["parametros"])
        ignorados = sorted(k for k in args if k not in aceitos)
        args_f = {k: v for k, v in args.items() if k in aceitos}
        v = self.guarda.checar(ator, ferramenta, args_f, autorizacao)
        if not v.ok:
            res = v.como_erro()
        else:
            fn = getattr(self.ferramentas, f"f_{ferramenta}")
            try:
                res = fn(ator, **args_f)
            except ciclo_vida.TransicaoInvalida as e:
                res = {"ok": False, "erro": "transicao_invalida", "mensagem": str(e)}
            except Exception as e:  # nunca derruba o agente: devolve erro estruturado
                res = {"ok": False, "erro": "erro_interno", "mensagem": f"{type(e).__name__}: {e}"[:500]}
            if res.get("ok") and "por_dia" in (meta.get("limites") or {}):
                self._contar_uso(ator, ferramenta)
        if ignorados:
            res["parametros_ignorados"] = ignorados
        self.log.registrar(self.est.agora_txt, rotulo or ator, "ferramenta", ferramenta=ferramenta,
                           tipo_ferramenta=meta["tipo"], args=args, resultado=res, autorizacao=autorizacao,
                           **({"ator_guarda": ator} if rotulo else {}))
        return res

    def uso_hoje(self, ator: str, ferramenta: str) -> int:
        r = self.est.q1("SELECT n FROM uso_ferramentas WHERE dia = ? AND ator = ? AND ferramenta = ?",
                        [self.est.agora.strftime("%Y-%m-%d"), ator, ferramenta])
        return r["n"] if r else 0

    def _contar_uso(self, ator: str, ferramenta: str) -> None:
        self.est.x("INSERT INTO uso_ferramentas (dia, ator, ferramenta, n) VALUES (?, ?, ?, 1) "
                   "ON CONFLICT (dia, ator, ferramenta) DO UPDATE SET n = n + 1",
                   [self.est.agora.strftime("%Y-%m-%d"), ator, ferramenta])

    # ------------------------------------------------------------------ consultas de apoio (guarda/ferramentas)
    def status_oc(self, oc_id: str | None) -> str | None:
        if self._oc_status is None:
            self._oc_status = dict(db.cursor(self.con).execute("SELECT oc_id, status FROM pedidos_compra").fetchall())
        return self._oc_status.get(oc_id) if oc_id else None

    def alteracoes_cadastro(self, fornecedor_id: str) -> list[dict]:
        """Histórico de alterações: CSV + o que mudou na simulação (a versão da simulação prevalece)."""
        if self._alt_csv is None:
            df = db.cursor(self.con).execute(
                "SELECT alteracao_id, strftime(data_hora, '%Y-%m-%d %H:%M:%S') AS data_hora, fornecedor_id, campo, "
                "valor_anterior, valor_novo, alterado_por, origem, confirmacao_registrada, confirmacao_como "
                "FROM alteracoes_cadastro").df()
            self._alt_csv = [{k: (None if v != v else v) for k, v in r.items()} for r in df.to_dict("records")]
        sim = {a["alteracao_id"]: a for a in self.est.q("SELECT * FROM alteracoes_sim WHERE fornecedor_id = ?", [fornecedor_id])}
        base = [sim.pop(a["alteracao_id"], a) for a in self._alt_csv if a["fornecedor_id"] == fornecedor_id]
        return sorted(base + list(sim.values()), key=lambda a: (str(a["data_hora"]), a["alteracao_id"]))

    def _sobrescrever_alteracao(self, a: dict, campos: dict) -> None:
        linha = {k: a.get(k) for k in ("alteracao_id", "data_hora", "fornecedor_id", "campo", "valor_anterior",
                                       "valor_novo", "alterado_por", "origem", "confirmacao_registrada",
                                       "confirmacao_como", "revertida_por", "revertida_em")}
        linha.update(campos)
        self.est.inserir("alteracoes_sim", linha, substituir=True)

    def marcar_alteracao_revertida(self, a: dict, ator: str) -> None:
        self._sobrescrever_alteracao(a, {"revertida_por": ator, "revertida_em": self.est.agora_txt})

    def registrar_confirmacao(self, a: dict, texto: str) -> None:
        self._sobrescrever_alteracao(a, {"confirmacao_registrada": "sim", "confirmacao_como": texto})

    def destino_contato(self, fornecedor_id: str, canal: str) -> str | None:
        f = self.est.fornecedor(fornecedor_id)
        if not f:
            return None
        if canal == "email":
            return f["email_cadastrado"]
        if canal == "telefone":
            return f["telefone_cadastrado"]
        if canal == "telefone_anterior":
            limite = (self.est.agora - timedelta(days=30)).strftime(FMT)
            recentes = [a for a in self.alteracoes_cadastro(fornecedor_id)
                        if a["campo"] == "telefone" and str(a["data_hora"]) >= limite and not a.get("revertida_por")]
            return recentes[0]["valor_anterior"] if recentes else f["telefone_cadastrado"]
        return None

    # ------------------------------------------------------------------ relógio e eventos
    def injetar_evento(self, quando: datetime | str, tipo: str, payload: dict, origem: str = "injetado") -> int:
        return self.relogio.agendar_evento(quando, tipo, payload, origem)

    def tique(self) -> dict:
        """Avança 1 hora e aplica tudo o que aconteceu. Não chama LLM."""
        brutos = self.relogio.avancar()
        aplicados = [self.aplicar_evento(e) for e in brutos]
        reav = self.reavaliar_pendentes()
        if reav.get("avaliados"):
            reav["saude"] = self.registrar_saude_cadeia()
        devidas =self.est.q("SELECT * FROM agenda_verificacoes WHERE status = 'devida' ORDER BY quando")
        return {"agora": self.est.agora_txt, "eventos": aplicados, "reavaliacao": reav,
                "verificacoes_devidas": devidas, "acabou": self.relogio.acabou()}

    def aplicar_evento(self, ev: dict) -> dict:
        tipo = ev.get("tipo")
        if tipo in ("lote_fechado", "fim_do_periodo"):
            return ev  # já aplicados pelo relógio
        h = getattr(self, f"_ev_{tipo}", None)
        p = ev.get("payload", {})
        if h is None:
            self.log.registrar(self.est.agora_txt, ev.get("origem") or "mundo", "evento_sem_tratador", tipo=tipo, payload=p)
            return {**ev, "aplicado": False}
        try:
            res = h(p, ev)
        except Exception as e:
            res = {"erro": f"{type(e).__name__}: {e}"}
        out = {"tipo": tipo, "origem": ev.get("origem"), **(res or {})}
        self.log.registrar(self.est.agora_txt, ev.get("origem") or "mundo", f"evento:{tipo}", **{k: v for k, v in out.items() if k != "tipo"})
        return out

    def _ev_retorno_banco(self, p, ev):
        return pos_lote.processar_retorno(self.est, self.log, p["lote_remessa"])

    def _ev_alteracao_cadastro(self, p, ev):
        fid = p["fornecedor_id"]
        f = self.est.fornecedor(fid)
        col = {"email": "email_cadastrado", "telefone": "telefone_cadastrado"}.get(p["campo"], p["campo"])
        anterior = f.get(col) if f else None
        if f and col in f:
            self.est.atualizar("fornecedores_vigente", "fornecedor_id", fid, {
                col: p["valor_novo"], "data_ultima_alteracao": self.est.agora_txt,
                "ultima_alteracao_por": p.get("alterado_por")})
        aid = self.est.proximo_id("ACS", "alteracoes_sim")
        self.est.inserir("alteracoes_sim", {
            "alteracao_id": aid, "data_hora": self.est.agora_txt, "fornecedor_id": fid, "campo": p["campo"],
            "valor_anterior": anterior, "valor_novo": p["valor_novo"], "alterado_por": p.get("alterado_por"),
            "origem": p.get("origem"), "confirmacao_registrada": p.get("confirmacao_registrada", "não"),
            "confirmacao_como": p.get("confirmacao_como")})
        afet = ciclo_vida.titulos_afetados(self.est, fornecedor_id=fid)
        ciclo_vida.marcar_reavaliacao(self.est, afet, f"cadastro de {fid} alterado ({p['campo']}, {aid})")
        return {"alteracao_id": aid, "fornecedor_id": fid, "campo": p["campo"], "titulos_reavaliar": afet}

    def _ev_nova_comunicacao(self, p, ev):
        cid = p.get("comunicacao_id") or self.est.proximo_id("EMS", "comunicacoes_sim")
        linha = {"comunicacao_id": cid, "data_hora": p.get("data_hora") or self.est.agora_txt}
        for k in ("canal", "remetente", "nome_remetente", "destinatario", "assunto", "texto", "anexos"):
            linha[k] = p.get(k)
        self.est.inserir("comunicacoes_sim", linha)
        # dependência: remetente igual a um canal do cadastro (ou mesmo domínio de e-mail)
        rem = (p.get("remetente") or "").lower()
        dom = rem.split("@")[-1] if "@" in rem else None
        fids = [f["fornecedor_id"] for f in self.est.q("SELECT fornecedor_id, email_cadastrado, telefone_cadastrado "
                                                       "FROM fornecedores_vigente")
                if rem and (rem in ((f["email_cadastrado"] or "").lower(), (f["telefone_cadastrado"] or "").lower())
                            or (dom and (f["email_cadastrado"] or "").lower().endswith("@" + dom)))]
        afet = sorted({t for fid in fids for t in ciclo_vida.titulos_afetados(self.est, fornecedor_id=fid)})
        ciclo_vida.marcar_reavaliacao(self.est, afet, f"comunicação nova {cid}")
        return {"comunicacao_id": cid, "fornecedores_relacionados": fids, "titulos_reavaliar": afet}

    def _ev_novo_titulo(self, p, ev):
        t = {k: p.get(k) for k in ("titulo_id", "fornecedor_id", "numero_nf_digitado", "oc_id", "parcela", "valor",
                                   "vencimento", "forma_pagamento", "linha_digitavel", "cnpj_beneficiario_boleto",
                                   "origem_lancamento", "lancado_por", "centro_custo", "historico")}
        t["data_lancamento"] = p.get("data_lancamento") or self.est.agora_txt
        t["nf_norm"] = nf_norm(t["numero_nf_digitado"])
        lote = p.get("lote_remessa")
        if not lote and t["vencimento"]:
            d = max(datetime.fromisoformat(str(t["vencimento"])).date(), self.est.agora.date())
            d = self.relogio.proximo_dia_util(d)
            sufixo = "LIT" if p.get("banco_pagador") == "Banco Litoral" else "MER"
            lote = f"RM{d.strftime('%Y%m%d')}-{sufixo}"
        aberto = lote and self.relogio.lote_aberto_para_alteracao(lote)[0]
        t.update({"status_erp": "agendado" if aberto else "aberto", "lote_remessa": lote if aberto else None,
                  "lote_original": lote if aberto else None, "estado": "agendado" if aberto else "lancado",
                  "banco_pagador": self.est.lote(lote)["banco_pagador"] if aberto else None,
                  "reavaliar": 1, "motivo_reavaliacao": "título novo", "atualizado_em": self.est.agora_txt})
        self.est.inserir("titulos", t)
        self.est.inserir("transicoes", {"titulo_id": t["titulo_id"], "de": None, "para": t["estado"],
                                        "quando": self.est.agora_txt, "ator": t.get("lancado_por") or "erp",
                                        "motivo": "lançamento"})
        return {"titulo_id": t["titulo_id"], "lote_remessa": t["lote_remessa"], "estado": t["estado"]}

    def _ev_resposta_fornecedor(self, p, ev):
        c = self.est.q1("SELECT * FROM contatos_fornecedor WHERE id = ?", [p["contato_id"]])
        r = self.responder_fornecedor(self, c)
        if not r:  # sem resposta
            self.est.atualizar("contatos_fornecedor", "id", c["id"], {"status": "sem_resposta"})
            return {"contato_id": c["id"], "respondeu": False}
        self.est.atualizar("contatos_fornecedor", "id", c["id"], {
            "resposta": r["texto"], "respondido_em": self.est.agora_txt, "status": "respondido"})
        cid = self.est.proximo_id("EMS", "comunicacoes_sim")
        self.est.inserir("comunicacoes_sim", {
            "comunicacao_id": cid, "data_hora": self.est.agora_txt,
            "canal": "email" if c["canal"] == "email" else "telefone", "remetente": c["destino"],
            "nome_remetente": r.get("nome_remetente"), "destinatario": "contasapagar@gogroup.com.br",
            "assunto": f"Re: contato {c['id']}", "texto": r["texto"], "anexos": None})
        afet = ciclo_vida.titulos_afetados(self.est, fornecedor_id=c["fornecedor_id"])
        for tid in afet:
            t = self.est.titulo(tid)
            if t["estado"] == "aguardando_fornecedor":
                ciclo_vida.transicionar(self.est, tid, "retido" if t["lote_original"] else "lancado", "fornecedor",
                                        f"resposta ao contato {c['id']}")
        ciclo_vida.marcar_reavaliacao(self.est, afet, f"fornecedor respondeu ({c['id']})")
        return {"contato_id": c["id"], "respondeu": True, "comunicacao_id": cid, "titulos_reavaliar": afet}

    def _ev_verificacao_agendada(self, p, ev):
        self.est.atualizar("agenda_verificacoes", "id", p["verificacao_id"], {"status": "devida"})
        return {"verificacao_id": p["verificacao_id"]}

    def _ev_resposta_humana(self, p, ev):
        """Decisão humana (Telegram, F5): vira chamada de ferramenta do humano."""
        res = self.executar(p["ator"], p["ferramenta"], p.get("args", {}))
        if p["ferramenta"] == "recusar_acao" and p.get("args", {}).get("motivo"):
            self.ferramentas.f_registrar_licao("sistema", f"{p['ator']} recusou {p['args'].get('solicitacao_id')}: "
                                                          f"{p['args']['motivo']}", origem="feedback_humano")
        return {"ator": p["ator"], "ferramenta": p["ferramenta"], "resultado": res}

    # ------------------------------------------------------------------ reavaliação reativa
    def reavaliar_pendentes(self, limite: int | None = REAVALIACOES_POR_TIQUE) -> dict:
        """Roda a cadeia nos títulos marcados (prioridade: lote que fecha antes, maior valor).

        Degrau 'código' da escada: título agendado PROVADO só em código (E1-E8 ok, sem comunicação ligada)
        vira 'certificado'; se uma reavaliação posterior não provar, volta para 'agendado'."""
        total = len(ciclo_vida.pendentes_reavaliacao(self.est))
        if self.avaliador is None:
            return {"pendentes": total, "avaliados": 0, "observacao": "sem avaliador"}
        cont = {"PROVADO": 0, "DUVIDOSO": 0, "QUEBRADO": 0}
        with self.est.transacao():  # um commit só (cada commit avulso custa um fsync)
            certificados, descertificados = self._reavaliar_lote(limite, cont)
        n = sum(cont.values())
        if n:
            self.log.registrar(self.est.agora_txt, "cadeia", "reavaliacao", avaliados=n, resultados=cont,
                               certificados=certificados, descertificados=descertificados, restantes=total - n)
        return {"pendentes": total, "avaliados": n, "resultados": cont, "certificados": certificados,
                "descertificados": descertificados, "restantes": total - n}

    def _reavaliar_lote(self, limite: int | None, cont: dict) -> tuple[int, int]:
        certificados = descertificados = 0
        for r in ciclo_vida.pendentes_reavaliacao(self.est, limite):
            tid = r["titulo_id"]
            res = self.avaliador(tid)
            ciclo_vida.registrar_avaliacao(self.est, tid, res.get("resultado"), res)
            cont[res.get("resultado")] = cont.get(res.get("resultado"), 0) + 1
            estado = self.est.titulo(tid)["estado"]
            provado = res.get("resultado") == "PROVADO" and not res.get("e9_pendente")
            if estado == "lancado":
                ciclo_vida.transicionar(self.est, tid, "cadeia_avaliada", "cadeia", f"cadeia: {res.get('resultado')}")
            elif estado in ("agendado", "reagendado") and provado:
                ciclo_vida.transicionar(self.est, tid, "certificado", "cadeia", "PROVADO só em código (E1-E8, sem E9)")
                certificados += 1
            elif estado == "certificado" and not provado:
                ciclo_vida.transicionar(self.est, tid, "agendado", "cadeia",
                                        f"reavaliação ({r['motivo_reavaliacao']}): {res.get('resultado')}"
                                        f"{', E9 pendente' if res.get('e9_pendente') else ''}")
                descertificados += 1
        return certificados, descertificados

    # ------------------------------------------------------------------ fontes e saúde (seção 1d)
    def verificar_fontes(self, con=None) -> list[dict]:
        res = fontes.verificar(con or db.cursor(self.con))
        self.fontes_com_drift = {r["fonte"] for r in res if not r["ok"]}
        for r in res:
            self.est.inserir("fontes_saude", {"quando": self.est.agora_txt, "fonte": r["fonte"],
                                              "canonico": r["canonico"], "versao": r["versao"],
                                              "ok": int(r["ok"]), "problemas": r["problemas"]})
            if not r["ok"]:
                self.log.registrar(self.est.agora_txt, "monitor_ingestao", "drift_fonte", fonte=r["fonte"],
                                   elos=r["elos"], problemas=r["problemas"])
        return res

    def registrar_saude_cadeia(self) -> dict:
        """% OK/QUEBRADO/INCONCLUSIVO por elo e por fonte nos títulos ainda em lote aberto; alarme de queda brusca."""
        cont: dict[tuple, dict] = {}
        for r in self.est.q("SELECT t.cadeia_json FROM titulos t JOIN lotes l USING (lote_remessa) "
                            "WHERE l.status = 'aberto' AND t.cadeia_json IS NOT NULL"):
            for e in json.loads(r["cadeia_json"]).get("elos", []):
                if e["elo"] == "E9":
                    continue
                chaves = [("elo", e["elo"])] + [("fonte", f) for f in fontes.FONTES_POR_ELO.get(e["elo"], [])]
                for k in chaves:
                    c = cont.setdefault(k, {"OK": 0, "QUEBRADO": 0, "INCONCLUSIVO": 0, "NAO_SE_APLICA": 0})
                    c[e["status"]] = c.get(e["status"], 0) + 1
        alarmes = []
        for (tipo, chave), c in cont.items():
            n = sum(c.values())
            aplicaveis = n - c["NAO_SE_APLICA"]
            pct = round(100 * c["OK"] / aplicaveis, 1) if aplicaveis else 100.0
            ant = self.est.q1("SELECT pct_ok, n FROM saude_cadeia WHERE tipo = ? AND chave = ? ORDER BY rowid DESC LIMIT 1",
                              [tipo, chave])
            self.est.inserir("saude_cadeia", {"quando": self.est.agora_txt, "tipo": tipo, "chave": chave, "n": n,
                                              "ok": c["OK"], "quebrado": c["QUEBRADO"], "inconclusivo": c["INCONCLUSIVO"],
                                              "nao_se_aplica": c["NAO_SE_APLICA"], "pct_ok": pct})
            if ant and aplicaveis >= 20 and ant["pct_ok"] - pct >= 20:
                alarmes.append({"tipo": tipo, "chave": chave, "de": ant["pct_ok"], "para": pct})
        if alarmes:
            # quase sempre é o DADO, não fraude: tratar como incidente de dados
            self.log.registrar(self.est.agora_txt, "monitor_ingestao", "queda_saude_cadeia", alarmes=alarmes)
        return {"alarmes": alarmes}

    # ------------------------------------------------------------------ visão geral
    def resumo(self) -> dict:
        prox = self.relogio.proximo_fechamento()
        em_jogo = None
        if prox:
            r = self.est.q1("SELECT count(*) n, round(coalesce(sum(valor), 0), 2) v FROM titulos "
                            "WHERE lote_remessa = ?", [prox["lote_remessa"]])
            em_jogo = {"lote": prox["lote_remessa"], "fecha_em": prox["fecha_em"], "qtd": r["n"], "valor": r["v"]}
        estados = {r["estado"]: r["n"] for r in self.est.q("SELECT estado, count(*) n FROM titulos GROUP BY 1 ORDER BY 2 DESC")}
        return {"agora": self.est.agora_txt, "proximo_lote": em_jogo, "estados": estados,
                "reavaliacao_pendente": len(ciclo_vida.pendentes_reavaliacao(self.est)),
                "lotes": self.est.q("SELECT lote_remessa, status, qtd_titulos, valor_total FROM lotes ORDER BY 1")}
