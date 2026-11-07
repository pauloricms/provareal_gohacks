"""Relógio simulado: 02/11 08:00 -> 09/11 08:00 em tiques de 1 hora.

A cada tique: (1) fecha os lotes cujo horário chegou (só em dia útil): o que estiver no lote SAI;
(2) devolve os eventos futuros vencidos (respostas, retornos do banco, eventos injetados) para o
Ambiente aplicar. No fim do período, os lotes que ainda não fecharam são avaliados pelo estado atual.
"""
from __future__ import annotations

import json
import unicodedata
from datetime import date, datetime, timedelta

from env import ciclo_vida, db
from env.estado import FMT, Estado
from env.eventos import LogEventos

def tesoureiro(politicas: dict) -> str:
    """Login de quem assina o lote quando ninguém age: a pessoa da política responsável por lotes de remessa."""
    pid = next((p["pessoa_id"] for p in politicas["acionamento_humano"]["pessoas"]
                if "lotes de remessa" in str(p.get("acionar_quando", ""))), None)
    con = db.conexao_leitura()
    try:
        r = con.execute("SELECT login FROM usuarios_sistema WHERE pessoa_id = ? AND sistema = 'ERP'", [pid]).fetchone()
    finally:
        con.close()
    return r[0] if r else "tesoureiro"


def carregar_feriados() -> set[date]:
    con = db.conexao_leitura()
    try:
        return {r[0] for r in con.execute(
            "SELECT data FROM feriados_bancarios WHERE sem_expediente_bancario = 'sim'").fetchall()}
    finally:
        con.close()


def nome_abreviado(texto: str | None, n: int = 20) -> str:
    s = unicodedata.normalize("NFKD", texto or "").encode("ascii", "ignore").decode().upper()
    return " ".join(s.split())[:n]


def destino_pagamento(titulo: dict, fornecedor: dict | None) -> str:
    """Para onde o dinheiro vai se nada mudar (mesmo formato de agenda_remessa.destino_pagamento)."""
    forma = titulo.get("forma_pagamento")
    if forma == "boleto":
        linha = titulo.get("linha_digitavel") or ""
        return f"boleto banco {linha[:3]}" if linha else "boleto sem linha digitável"
    f = fornecedor or {}
    if forma == "ted":
        return f"ted {f.get('banco')}/{f.get('agencia')}/{f.get('conta')}"
    if forma == "pix":
        return f"pix {f.get('chave_pix')}"
    return f"{forma} (forma desconhecida)"


class Relogio:
    def __init__(self, est: Estado, log: LogEventos, politicas: dict):
        self.est = est
        self.log = log
        per = politicas["periodo_simulado"]
        self.inicio = datetime.fromisoformat(per["inicio"])
        self.fim = datetime.fromisoformat(per["fim"])
        self.feriados = carregar_feriados()
        self.tesoureiro = tesoureiro(politicas)

    # ------------------------------------------------------------------ calendário
    @property
    def agora(self) -> datetime:
        return self.est.agora

    def dia_util(self, d: date) -> bool:
        return d.weekday() < 5 and d not in self.feriados

    def proximo_dia_util(self, d: date) -> date:
        while not self.dia_util(d):
            d += timedelta(days=1)
        return d

    def acabou(self) -> bool:
        return self.agora >= self.fim

    def horario_comercial(self, t: datetime) -> bool:
        return self.dia_util(t.date()) and 8 <= t.hour < 18

    def proximo_horario_comercial(self, t: datetime) -> datetime:
        """Primeiro instante em horário comercial a partir de t (arredondado para o tique)."""
        t = t.replace(minute=0, second=0, microsecond=0)
        while not self.horario_comercial(t):
            t += timedelta(hours=1)
        return t

    def proximo_fechamento(self) -> dict | None:
        agora = self.agora_txt()
        for l in self.est.q("SELECT * FROM lotes WHERE status = 'aberto' ORDER BY data_pagamento, horario_fechamento"):
            if self._fechamento(l).strftime(FMT) > agora:
                return {**l, "fecha_em": self._fechamento(l).strftime(FMT)}
        return None

    def agora_txt(self) -> str:
        return self.est.agora_txt

    @staticmethod
    def _fechamento(lote: dict) -> datetime:
        return datetime.fromisoformat(f"{lote['data_pagamento']} {lote['horario_fechamento']}:00")

    def lote_aberto_para_alteracao(self, lote_remessa: str) -> tuple[bool, str]:
        l = self.est.lote(lote_remessa)
        if l is None:
            return False, f"lote {lote_remessa} não existe"
        if l["status"] != "aberto":
            return False, f"lote {lote_remessa} já está {l['status']}"
        if self._fechamento(l) <= self.agora:
            return False, f"lote {lote_remessa} já passou do horário de fechamento"
        return True, ""

    # ------------------------------------------------------------------ avanço
    def avancar(self) -> list[dict]:
        """Avança 1 hora. Devolve [fechamentos de lote + eventos futuros vencidos]."""
        if self.acabou():
            return []
        novo = self.agora + timedelta(hours=1)
        self.est.set_meta("agora", novo.strftime(FMT))
        self.log.registrar(novo.strftime(FMT), "relogio", "tique")
        saida: list[dict] = []
        for l in self.est.q("SELECT * FROM lotes WHERE status = 'aberto'"):
            if self._fechamento(l) == novo and self.dia_util(novo.date()):
                saida.append(self.fechar_lote(l["lote_remessa"]))
        for ev in self.est.q("SELECT * FROM eventos_futuros WHERE processado_em IS NULL AND quando <= ? "
                             "ORDER BY quando, id", [novo.strftime(FMT)]):
            self.est.atualizar("eventos_futuros", "id", ev["id"], {"processado_em": novo.strftime(FMT)})
            saida.append({"tipo": ev["tipo"], "quando": ev["quando"], "origem": ev["origem"],
                          "payload": json.loads(ev["payload"] or "{}"), "evento_id": ev["id"]})
        if self.acabou():
            saida.append(self.finalizar())
        return saida

    def agendar_evento(self, quando: datetime | str, tipo: str, payload: dict, origem: str) -> int:
        q = quando if isinstance(quando, str) else quando.strftime(FMT)
        return self.est.inserir("eventos_futuros", {"quando": q, "tipo": tipo, "payload": payload, "origem": origem})

    # ------------------------------------------------------------------ fechamento
    def fechar_lote(self, lote_remessa: str) -> dict:
        from env import pos_lote  # evita import circular

        est = self.est
        l = est.lote(lote_remessa)
        agora = est.agora_txt
        assinado_por = l["assinado_por"] or self.tesoureiro
        assinatura = "liberado por aprovador" if l["assinado_por"] else "tesoureiro assinou sem conferir (padrão)"
        enviados, retidos = [], []
        with est.transacao():
            for t in est.q("SELECT * FROM titulos WHERE lote_remessa = ?", [lote_remessa]):
                f = est.fornecedor(t["fornecedor_id"])
                if t["bloqueado"] or (f and f["bloqueado"]) or t["estado"] not in ciclo_vida.NA_FILA:
                    motivo = "fornecedor bloqueado" if (f and f["bloqueado"]) else (
                        t["motivo_bloqueio"] or f"estado {t['estado']}")
                    para = "retido" if t["estado"] in ciclo_vida.NA_FILA else t["estado"]
                    ciclo_vida.transicionar(est, t["titulo_id"], para, "relogio",
                                            f"não saiu no {lote_remessa}: {motivo}",
                                            campos={"lote_remessa": None, "status_erp": "aberto"})
                    retidos.append({"titulo_id": t["titulo_id"], "motivo": motivo})
                    continue
                destino = destino_pagamento(t, f)
                est.inserir("lote_saida", {
                    "lote_remessa": lote_remessa, "titulo_id": t["titulo_id"], "fornecedor_id": t["fornecedor_id"],
                    "valor": t["valor"], "forma_pagamento": t["forma_pagamento"], "destino": destino,
                    "enviado_em": agora,
                })
                ciclo_vida.transicionar(est, t["titulo_id"], "enviado", "relogio", f"saiu no {lote_remessa}",
                                        campos={"banco_pagador": l["banco_pagador"]})
                enviados.append({"titulo_id": t["titulo_id"], "valor": t["valor"], "destino": destino})
            total = round(sum(e["valor"] for e in enviados), 2)
            est.atualizar("lotes", "lote_remessa", lote_remessa, {
                "status": "fechado", "assinado_por": assinado_por, "assinado_em": l["assinado_em"] or agora,
                "fechado_em": agora, "qtd_titulos": len(enviados), "valor_total": total,
            })
        retorno_em = pos_lote.agendar_retorno(self, l)
        ev = {"tipo": "lote_fechado", "lote_remessa": lote_remessa, "banco_pagador": l["banco_pagador"],
              "assinado_por": assinado_por, "assinatura": assinatura, "qtd_enviados": len(enviados),
              "valor_enviado": total, "retidos": retidos, "retorno_previsto": retorno_em}
        self.log.registrar(agora, "relogio", "lote_fechado", **{k: v for k, v in ev.items() if k != "tipo"},
                           titulos=[e["titulo_id"] for e in enviados])
        return ev

    def finalizar(self) -> dict:
        """Fim do período: lotes ainda abertos são avaliados pelo estado em que estão agora."""
        est = self.est
        agora = est.agora_txt
        resumo = []
        with est.transacao():
            for l in est.q("SELECT * FROM lotes WHERE status = 'aberto'"):
                tits = est.q("SELECT t.*, f.bloqueado AS forn_bloq FROM titulos t "
                             "LEFT JOIN fornecedores_vigente f USING (fornecedor_id) "
                             "WHERE t.lote_remessa = ?", [l["lote_remessa"]])
                n, v = 0, 0.0
                for t in tits:
                    if t["bloqueado"] or t["forn_bloq"] or t["estado"] not in ciclo_vida.NA_FILA:
                        continue
                    est.inserir("lote_saida", {
                        "lote_remessa": l["lote_remessa"], "titulo_id": t["titulo_id"],
                        "fornecedor_id": t["fornecedor_id"], "valor": t["valor"],
                        "forma_pagamento": t["forma_pagamento"],
                        "destino": destino_pagamento(t, est.fornecedor(t["fornecedor_id"])),
                        "enviado_em": None, "situacao_fim": "previsto (avaliado no fim do período)",
                    })
                    n += 1
                    v += t["valor"]
                est.atualizar("lotes", "lote_remessa", l["lote_remessa"],
                              {"status": "avaliado_no_fim", "qtd_titulos": n, "valor_total": round(v, 2)})
                resumo.append({"lote_remessa": l["lote_remessa"], "qtd": n, "valor": round(v, 2)})
        self.log.registrar(agora, "relogio", "fim_do_periodo", lotes=resumo)
        return {"tipo": "fim_do_periodo", "lotes": resumo}
