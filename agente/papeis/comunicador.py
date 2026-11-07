"""Comunicador: tudo o que precisa de gente vira UM card por pessoa e por lote (nunca uma mensagem por título).

Silencioso por padrão: o card só sai quando o lote está para fechar (até 2 h antes) ou, sem lote, às 17h.
Cada card = um acionamento (R$ 60 se desnecessário). Telegram entra na F4; até lá o card fica no quadro."""
from __future__ import annotations

import json
from datetime import datetime, timedelta

from agente import quadro

JANELA_H = 2
HORA_DIARIA = 17


def enfileirar(amb, pessoa_id: str | None, caso_id: str, titulos: list[str], item: str,
               solicitacao_id: str | None = None) -> None:
    est = amb.est
    lotes = []
    for tid in titulos:
        t = est.titulo(tid)
        if t and (t["lote_remessa"] or t["lote_original"]):
            lotes.append(t["lote_remessa"] or t["lote_original"])
    est.inserir("fila_comunicador", {"pessoa_id": pessoa_id or amb.guarda.pessoa_coord,
                                     "lote_remessa": min(lotes) if lotes else None,
                                     "caso_id": caso_id, "titulos": titulos, "item": item,
                                     "solicitacao_id": solicitacao_id, "criada_em": est.agora_txt})


def enviar(amb, forcar: bool = False) -> list[dict]:
    est = amb.est
    pend = est.q("SELECT * FROM fila_comunicador WHERE card_id IS NULL")
    grupos: dict[tuple, list[dict]] = {}
    for p in pend:
        grupos.setdefault((p["pessoa_id"], p["lote_remessa"]), []).append(p)
    cards = []
    for (pessoa, lote_id), itens in grupos.items():
        lote = est.lote(lote_id) if lote_id else None
        if lote:
            fecha = datetime.fromisoformat(f"{lote['data_pagamento']} {lote['horario_fechamento']}:00")
            pronto = forcar or lote["status"] != "aberto" or fecha - est.agora <= timedelta(hours=JANELA_H)
            quando = f"lote {lote_id} (fecha {lote['data_pagamento']} {lote['horario_fechamento']})"
        else:
            pronto = forcar or est.agora.hour >= HORA_DIARIA
            quando = "sem lote (resumo do dia)"
        if not pronto:
            continue
        info = amb.guarda.pessoas.get(pessoa, {"nome": pessoa, "cargo": ""})
        linhas = [f"@{info['nome']} ({info['cargo']}): {len(itens)} decisão(ões) para {quando}"]
        todos_titulos: list[str] = []
        for i, it in enumerate(itens, 1):
            tits = json.loads(it["titulos"] or "[]")
            todos_titulos += tits
            valor = sum((est.titulo(t) or {}).get("valor") or 0 for t in tits)
            sol = f" [{it['solicitacao_id']}]" if it["solicitacao_id"] else ""
            linhas.append(f"{i}. {it['caso_id']}{sol} · {', '.join(tits)} · R$ {valor:,.2f} · {it['item']}")
        texto = "\n".join(linhas)
        urg = "alta" if lote and lote["status"] == "aberto" else "media"
        r = amb.executar("agente", "acionar_pessoa", {"pessoa_id": pessoa, "motivo": texto[:3000], "urgencia": urg,
                                                      "titulos": sorted(set(todos_titulos))}, rotulo="comunicador")
        cid = est.proximo_id("CARD", "cards")
        est.inserir("cards", {"id": cid, "pessoa_id": pessoa, "lote_remessa": lote_id, "itens": [dict(i) for i in itens],
                              "texto": texto, "criado_em": est.agora_txt, "acionamento_id": r.get("acionamento_id"),
                              "status": "enviado" if r.get("ok") else f"erro: {r.get('erro')}"})
        for it in itens:
            est.atualizar("fila_comunicador", "id", it["id"], {"card_id": cid})
        amb.log.registrar(est.agora_txt, "comunicador", "card_escalada", card_id=cid, pessoa_id=pessoa,
                          lote_remessa=lote_id, itens=len(itens), texto=texto, acionamento=r.get("acionamento_id"))
        cards.append({"card_id": cid, "pessoa_id": pessoa, "itens": len(itens)})
    return cards
