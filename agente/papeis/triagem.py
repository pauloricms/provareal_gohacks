"""Triagem (código, sem LLM): o que o código não resolveu vira CASO no quadro, um por fornecedor.

Fontes: títulos de lotes próximos com cadeia QUEBRADO/DUVIDOSO, alertas de detectores e verificações agendadas.
Um título já tratado só volta se a cadeia dele MUDOU (assinatura diferente) ou se uma verificação pediu."""
from __future__ import annotations

import json
from datetime import timedelta

from agente import quadro
from env import ciclo_vida

ABERTOS = ("triado", "em_investigacao")
TRATADOS = ("decidido", "aguardando_aprovacao", "inconclusivo") + ABERTOS


def assinatura(cj: dict) -> str:
    return f"{cj.get('resultado')}|{','.join(cj.get('quebrados', []))}|{','.join(cj.get('inconclusivos', []))}"


def triar(amb, alertas: list[dict], verificacoes: list[dict], horizonte_h: int) -> list[dict]:
    est = amb.est
    limite = (est.agora + timedelta(hours=horizonte_h)).strftime("%Y-%m-%d %H:%M")
    agora = est.agora.strftime("%Y-%m-%d %H:%M")
    na_fila = ",".join(f"'{s}'" for s in ciclo_vida.NA_FILA)
    sinal: dict[str, str] = {}
    for r in est.q(f"""SELECT t.titulo_id FROM titulos t JOIN lotes l USING (lote_remessa)
                       WHERE l.status = 'aberto' AND (l.data_pagamento || ' ' || l.horario_fechamento) <= ?
                       AND (l.data_pagamento || ' ' || l.horario_fechamento) > ?
                       AND t.estado IN ({na_fila}) AND t.cadeia_resultado IN ('QUEBRADO', 'DUVIDOSO')""",
                   [limite, agora]):
        sinal[r["titulo_id"]] = "cadeia"
    for a in alertas:
        t = est.titulo(a["chave"]) if a.get("chave") else None
        if t and t["estado"] in ciclo_vida.NA_FILA:
            sinal.setdefault(t["titulo_id"], f"detector:{a['detector_id']}")
    forcados = set()
    for v in verificacoes:
        t = est.titulo(v["alvo"])
        est.atualizar("agenda_verificacoes", "id", v["id"], {"status": "executada", "executada_em": est.agora_txt})
        if t and t["estado"] in ciclo_vida.NA_FILA | {"retido", "aguardando_fornecedor"}:
            sinal[t["titulo_id"]] = f"verificacao:{v['id']}"
            forcados.add(t["titulo_id"])

    travados = quadro.titulos_travados(est)
    tratados: dict[str, dict] = {}
    for c in est.q(f"SELECT id, titulo_ids, assinaturas, status FROM casos WHERE status IN "
                   f"({','.join(repr(s) for s in TRATADOS)})"):
        ass = json.loads(c["assinaturas"] or "{}")
        for tid in json.loads(c["titulo_ids"] or "[]"):
            tratados[tid] = {"caso": c["id"], "status": c["status"], "assinatura": ass.get(tid)}

    por_forn: dict[str, list[tuple[str, dict, str]]] = {}
    for tid, origem in sinal.items():
        t = est.titulo(tid)
        cj = json.loads(t["cadeia_json"]) if t["cadeia_json"] else {}
        ass = assinatura(cj)
        ja = tratados.get(tid)
        if tid in travados and not (ja and ja["status"] == "triado"):
            continue
        if ja and tid not in forcados and (ja["status"] in ABERTOS or ja["assinatura"] == ass):
            continue
        por_forn.setdefault(t["fornecedor_id"], []).append((tid, t, origem))

    for fid, itens in por_forn.items():
        aberto = est.q1("SELECT * FROM casos WHERE fornecedor_id = ? AND status = 'triado'", [fid])
        tids = sorted({i[0] for i in itens} | (set(json.loads(aberto["titulo_ids"])) if aberto else set()))
        ts = [est.titulo(x) for x in tids]
        lotes = [est.lote(t["lote_remessa"]) for t in ts if t["lote_remessa"]]
        prazo = min((f"{l['data_pagamento']} {l['horario_fechamento']}" for l in lotes), default=None)
        lote_prox = min(lotes, key=lambda l: (l["data_pagamento"], l["horario_fechamento"]))["lote_remessa"] if lotes else None
        assinaturas = {t["titulo_id"]: assinatura(json.loads(t["cadeia_json"]) if t["cadeia_json"] else {}) for t in ts}
        campos = {"titulo_ids": tids, "fornecedor_id": fid, "valor_em_jogo": round(sum(t["valor"] for t in ts), 2),
                  "prazo": prazo, "lote_remessa": lote_prox, "assinaturas": assinaturas,
                  "origem": ",".join(sorted({i[2] for i in itens})), "atualizado_em": est.agora_txt}
        if aberto:
            est.atualizar("casos", "id", aberto["id"], campos)
        else:
            cid = est.proximo_id("CASO", "casos")
            est.inserir("casos", {"id": cid, "status": "triado", "mesa": "integridade", "tentativas": 0,
                                  "criado_em": est.agora_txt, **campos})
            amb.log.registrar(est.agora_txt, "triagem", "caso_aberto", caso_id=cid, fornecedor_id=fid,
                              titulos=tids, origem=campos["origem"], valor_em_jogo=campos["valor_em_jogo"], prazo=prazo)
    return est.q("SELECT * FROM casos WHERE status = 'triado' ORDER BY prazo, valor_em_jogo DESC")
