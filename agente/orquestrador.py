"""Orquestrador: o tique do time de agentes (seção 2).

    python -m agente.orquestrador --ate "2026-11-03 10:00"

Escada de decisão: código (cadeia, detectores, agenda) -> leitor leve (E9) -> Chefe -> Investigador + Crítico
-> Executor (código + guarda) -> Comunicador (um card por pessoa e lote). Tique sem pendência não chama LLM.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from pathlib import Path

from agente import gravacao, quadro
from agente.esquemas import ACOES_IRREVERSIVEIS, ACOES_REVERSIVEIS_BARATAS, Proposta, Veredito
from agente.llm import LLM, MODELOS, POOLS, USAR_POOL, ErroLLM
from agente.papeis import (chefe, comunicador, critico, curador, executor, investigador, leitor_e9, pontualidade,
                           relacionamento, triagem)
from env.ambiente import Ambiente

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")


class Orquestrador:
    def __init__(self, amb: Ambiente, llm: LLM | None = None, orcamento: int = 25, paralelo: int = 4,
                 horizonte_h: int = 30, max_e9: int = 8, orcamento_critico: int = 3, max_rel: int = 4,
                 gravar: bool = True):
        self.amb = amb
        quadro.iniciar(amb.est)
        self.llm = llm or LLM(registrar=lambda agente, tipo, **kw: amb.log.registrar(amb.est.agora_txt, agente, tipo, **kw))
        self.orcamento, self.paralelo, self.horizonte_h = orcamento, paralelo, horizonte_h
        self.max_e9, self.orc_critico, self.max_rel = max_e9, orcamento_critico, max_rel
        self._adiados_log: set = set()
        self.gravador = gravacao.Gravador(amb) if gravar else None
        if self.gravador is not None and hasattr(self.llm, "gravar"):
            self.llm.gravar = self.gravador.chamada_llm
        amb.log.registrar(amb.est.agora_txt, "orquestrador", "configuracao", modelos=MODELOS,
                          pools=POOLS if USAR_POOL else None, orcamento=orcamento,
                          paralelo=paralelo, horizonte_h=horizonte_h)

    # ------------------------------------------------------------------ um caso (roda em paralelo)
    def _caso(self, caso: dict, orc: int) -> dict:
        amb = self.amb
        try:
            prop, _ = investigador.investigar(self.llm, amb, caso, orc)
            versoes = [(prop, None)]
            chamar, motivo = self._precisa_critico(prop)
            if not chamar:
                amb.log.registrar(amb.est.agora_txt, "orquestrador", "critico_dispensado", caso_id=caso["id"],
                                  motivo=motivo, acoes=[a.ferramenta for a in prop.acao_escolhida])
            if chamar:
                ver = critico.criticar(self.llm, amb, caso["id"], prop, self.orc_critico)
                versoes[-1] = (prop, ver)
                if not ver.reproduziu:
                    amb.log.registrar(amb.est.agora_txt, "critico", "critico_refutou", caso_id=caso["id"],
                                      objecoes=ver.objecoes)
                    prop2, _ = investigador.investigar(self.llm, amb, caso, max(3, orc // 2), objecoes=ver.objecoes,
                                                       proposta_anterior=prop.model_dump())
                    ver2 = critico.criticar(self.llm, amb, caso["id"], prop2, self.orc_critico) \
                        if self._precisa_critico(prop2)[0] else None
                    versoes.append((prop2, ver2))
                    if ver2 is not None and not ver2.reproduziu:
                        amb.log.registrar(amb.est.agora_txt, "critico", "critico_refutou", caso_id=caso["id"],
                                          objecoes=ver2.objecoes, segunda_vez=True)
                        return {"status": "inconclusivo", "versoes": versoes}
            return {"status": "aprovada", "versoes": versoes}
        except ErroLLM as e:
            amb.log.registrar(amb.est.agora_txt, "orquestrador", "llm_falha", caso_id=caso["id"], erro=str(e)[:300])
            return {"status": "falha", "versoes": [], "erro": str(e)[:300]}

    def _quebrado_por_codigo(self, tid: str | None) -> bool:
        """Evidência forte = algum elo E1-E8 QUEBRADO calculado pelo código (não depende de leitura de LLM)."""
        t = self.amb.est.titulo(tid) if tid else None
        if not t or not t["cadeia_json"]:
            return False
        return any(e != "E9" for e in json.loads(t["cadeia_json"]).get("quebrados", []))

    def _precisa_critico(self, prop: Proposta) -> tuple[bool, str]:
        """Regra de custo do Crítico (seção 10). Devolve (chamar?, motivo)."""
        acoes = prop.acao_escolhida
        if not acoes:
            if any((self.amb.est.titulo(t) or {}).get("cadeia_resultado") == "QUEBRADO" for t in prop.titulos):
                return True, "deixar sair título com elo QUEBRADO"
            return False, "nenhuma ação e nenhum elo quebrado"
        for a in acoes:
            if a.ferramenta in ACOES_IRREVERSIVEIS:
                return True, f"ação irreversível ou cara ({a.ferramenta})"
            req, _ = self.amb.guarda.requisitos("agente", a.ferramenta, dict(a.parametros))
            if req:
                return True, f"{a.ferramenta} acima da alçada ({', '.join(req)})"
            if a.ferramenta not in ACOES_REVERSIVEIS_BARATAS:
                return True, f"{a.ferramenta} não é reversível e barata"
            if a.ferramenta == "retirar_do_lote" and not self._quebrado_por_codigo(a.parametros.get("titulo_id")):
                return True, "retirar do lote sem elo quebrado comprovado pelo código"
        return False, "ações reversíveis e baratas com elo quebrado comprovado pelo código"

    def _registrar_versoes(self, caso: dict, versoes: list[tuple[Proposta, Veredito | None]]) -> None:
        est = self.amb.est
        for i, (p, v) in enumerate(versoes, 1):
            pid = quadro.nova_proposta(est, tarefa_id=caso.get("_tarefa"), caso_id=caso["id"], versao=i,
                                       agente="investigador", modelo="pool:decisao" if USAR_POOL else MODELOS["decisao"],
                                       conteudo=p.model_dump(),
                                       status="aprovada" if (v is None or v.reproduziu) else "refutada")
            if v is not None:
                quadro.novo_veredito(est, proposta_id=pid, caso_id=caso["id"], agente="critico",
                                     modelo=MODELOS["critico"], reproduziu=int(v.reproduziu), conteudo=v.model_dump())

    # ------------------------------------------------------------------ o tique
    def tique(self) -> dict:
        amb, est = self.amb, self.amb.est
        t0 = time.time()
        llm0 = dict(self.llm.stats)
        res = amb.tique()                                     # relógio, eventos, cadeia (código)
        alertas = amb.ferramentas.rodar_detectores()          # detectores salvos (código)
        if alertas:
            amb.log.registrar(est.agora_txt, "detectores", "alertas", alertas=[
                {"detector": a["detector_id"], "chave": a["chave"]} for a in alertas])

        # leitor de comunicações (E9): LLM leve, por fornecedor, com cache
        e9 = []
        pend_e9 = list(leitor_e9.pendentes(amb, self.horizonte_h).items())[: self.max_e9]
        if pend_e9:
            with ThreadPoolExecutor(self.paralelo) as ex:
                futs = [ex.submit(self._ler_e9, f, ts) for f, ts in pend_e9]
                e9 = [f.result() for f in futs]
            amb.reavaliar_pendentes()                         # cadeia de novo com o E9 lido -> certifica

        # triagem (código) -> Chefe (LLM, só se houver escolha) -> Investigadores em paralelo -> Crítico
        casos = self._prazo_manda(triagem.triar(amb, alertas, res["verificacoes_devidas"], self.horizonte_h))
        plano =chefe.planejar(self.llm, amb, casos, self.orcamento, self.paralelo) if casos else []
        for c, _, _ in plano:
            est.atualizar("casos", "id", c["id"], {"status": "em_investigacao", "atualizado_em": est.agora_txt})
            quadro.travar(est, json.loads(c["titulo_ids"]), c["id"])
        resultados = []
        if plano:
            with ThreadPoolExecutor(self.paralelo) as ex:
                futs = [(c, ex.submit(self._caso, c, orc)) for c, orc, _ in plano]
                resultados = [(c, f.result()) for c, f in futs]

        # Executor (código + guarda), sequencial
        decididos = []
        for c, r in resultados:
            if r["status"] == "falha":
                est.atualizar("casos", "id", c["id"], {"status": "triado", "atualizado_em": est.agora_txt})
                quadro.destravar(est, c["id"])
                continue
            self._registrar_versoes(c, r["versoes"])
            prop = r["versoes"][-1][0]
            custo = min((a.custo_esperado for a in prop.acoes_candidatas), default=None)
            campos = {"hipotese": prop.hipotese, "evidencias": prop.evidencias, "p_problema": prop.p_problema,
                      "acoes_candidatas": [a.model_dump() for a in prop.acoes_candidatas],
                      "acao_escolhida": [a.model_dump() for a in prop.acao_escolhida], "custo_esperado": custo,
                      "tentativas": len(r["versoes"]), "atualizado_em": est.agora_txt}
            if r["status"] == "inconclusivo":
                est.atualizar("casos", "id", c["id"], {**campos, "status": "inconclusivo",
                                                       "desfecho": "Crítico refutou duas vezes: regra do prazo decide"})
                quadro.destravar(est, c["id"])
                continue
            acoes = executor.executar(amb, c, prop)
            espera = any("aprovacao" in a for a in acoes)
            est.atualizar("casos", "id", c["id"], {
                **campos, "status": "aguardando_aprovacao" if espera else "decidido",
                "desfecho": "segue no lote (legítimo)" if not prop.acao_escolhida else
                f"{len(acoes)} ação(ões) executadas pelo guarda"})
            quadro.destravar(est, c["id"])
            decididos.append({"caso_id": c["id"], "acoes": [a.get("ferramenta") for a in acoes], "espera": espera})

        pont = pontualidade.rodar(amb)                        # mesa 2: o legítimo volta ao lote a tempo
        rel = relacionamento.rodar(self.llm, amb, limite=self.max_rel, paralelo=self.paralelo)  # mesa 3
        prazo = executor.regra_do_prazo(amb)                  # watchdog: lote fechando com título não provado
        cur = curador.curar(self.llm, amb) if est.agora.hour == curador.HORA else None  # fim do dia
        cards = comunicador.enviar(amb)                       # um card por pessoa e lote
        d = {k: round(self.llm.stats[k] - llm0[k], 2) for k in self.llm.stats}
        resumo = {"agora": est.agora_txt, "eventos": [e.get("tipo") for e in res["eventos"]],
                  "reavaliados": res["reavaliacao"].get("avaliados"), "alertas_detector": len(alertas),
                  "leituras_e9": len(e9), "casos_pendentes": len(casos), "investigados": len(plano),
                  "decididos": decididos, "pontualidade": len(pont), "relacionamento": len(rel),
                  "regra_do_prazo": len(prazo), "curadoria": cur, "cards": cards, "llm": d,
                  "segundos": round(time.time() - t0, 1)}
        est.inserir("orcamento_tique", {"quando": est.agora_txt, "chamadas_llm": int(d["chamadas"]), "detalhe": resumo},
                    substituir=True)
        amb.log.registrar(est.agora_txt, "orquestrador", "tique_resumo", **resumo)
        if self.gravador is not None:
            try:
                self.gravador.fim_do_tique()                  # F3d: fotografia, llm/, lotes/ (escrita atômica)
            except Exception as e:                            # gravação nunca derruba a simulação
                amb.log.registrar(est.agora_txt, "orquestrador", "gravacao_falhou", erro=str(e)[:300])
        return resumo

    def _prazo_manda(self, casos: list[dict]) -> list[dict]:
        """9c.3: o orçamento do tique vai primeiro para o lote que fecha antes. Casos com prazo depois do primeiro
        fechamento esperam (evento caso_adiado), salvo os forçados por verificação agendada. Se nenhum caso é do
        próximo fechamento, todos seguem (não há o que priorizar)."""
        if not casos:
            return casos
        primeiro = min((c["prazo"] for c in casos if c["prazo"]), default=None)
        if primeiro is None:
            return casos
        seguem, adiados = [], []
        for c in casos:
            urgente = c["prazo"] == primeiro or "verificacao" in (c["origem"] or "")
            (seguem if urgente else adiados).append(c)
        for c in adiados:
            chave = (c["id"], c["prazo"])
            if chave not in self._adiados_log:
                self._adiados_log.add(chave)
                self.amb.log.registrar(self.amb.est.agora_txt, "orquestrador", "caso_adiado", caso_id=c["id"],
                                       prazo=c["prazo"], valor_em_jogo=c["valor_em_jogo"],
                                       motivo=f"lote que fecha antes ({primeiro}) tem prioridade no orçamento")
        return seguem

    def _ler_e9(self, fornecedor_id: str, titulos: list[str]) -> dict:
        try:
            return leitor_e9.ler(self.llm, self.amb, fornecedor_id, titulos)
        except ErroLLM as e:
            self.amb.log.registrar(self.amb.est.agora_txt, "leitor_e9", "llm_falha", fornecedor_id=fornecedor_id,
                                   erro=str(e)[:300])
            return {"fornecedor_id": fornecedor_id, "erro": str(e)[:200]}

    def rodar(self, ate: datetime, verboso: bool = True) -> None:
        while self.amb.est.agora < ate and not self.amb.relogio.acabou():
            r = self.tique()
            if verboso:
                ev = [e for e in r["eventos"] if e not in ("verificacao_agendada",)]
                print(f"{r['agora']}  {r['segundos']:5.1f}s  llm={int(r['llm']['chamadas']):2d}  "
                      f"e9={r['leituras_e9']}  casos={r['casos_pendentes']} invest={r['investigados']} "
                      f"decididos={len(r['decididos'])} pont={r['pontualidade']} rel={r['relacionamento']} "
                      f"prazo={r['regra_do_prazo']} "
                      f"cards={len(r['cards'])}"
                      f"{'  ' + ','.join(ev) if ev else ''}", flush=True)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--ate", default="2026-11-03 10:00")
    ap.add_argument("--run", help="continua uma execução existente (pasta em runs/)")
    ap.add_argument("--orcamento", type=int, default=25)
    ap.add_argument("--paralelo", type=int, default=4)
    a = ap.parse_args()
    amb = Ambiente(Path(a.run)) if a.run else Ambiente.novo()
    print(f"execução: {amb.run_dir}  modelos: {MODELOS}", flush=True)
    orq = Orquestrador(amb, orcamento=a.orcamento, paralelo=a.paralelo)
    orq.rodar(datetime.fromisoformat(a.ate))
    print(f"\nLLM: {orq.llm.stats}\ntrilha: {amb.log.caminho}")


if __name__ == "__main__":
    main()
