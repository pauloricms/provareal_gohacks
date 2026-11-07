"""Gera o painel em HTML/JS (replay) de uma execução: `runs/<ts>/painel.html`, com os dados embutidos.
Abre direto no navegador, sem servidor. Só lê `runs/<ts>/` (fotografias, eventos, estado, resumo).
O modelo da página fica em `app/painel.html` (sem dados); o arquivo gerado fica em runs/ (fora do git).

    python -m app.painel [runs/<ts>]          # sem argumento: a execução mais recente com fotografias
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

from agente.gravacao import ler_eventos, numero_tique

RAIZ = Path(__file__).resolve().parents[1]
SAIU = {"enviado", "retorno_banco", "conciliado", "baixado"}
FORA = {"retido", "bloqueado", "aguardando_aprovacao", "aguardando_fornecedor", "reagendado"}
ORIGEM = {"cadeia": "elo da cadeia quebrado ou duvidoso", "detector": "alerta de detector",
          "verificacao": "verificação agendada"}


def brl(v) -> str:
    return "R$ " + f"{float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def codigo(t: dict) -> str:
    e = t.get("estado")
    if e in SAIU:
        return "S"
    if e == "cancelado":
        return "C"
    if e in FORA:
        return "R"
    return {"PROVADO": "P", "DUVIDOSO": "D", "QUEBRADO": "Q"}.get(t.get("resultado"), "D")


def _origem(o: str) -> str:
    return ", ".join(sorted({ORIGEM.get(x.split(":")[0], x) for x in (o or "").split(",") if x}))


def gerar(run: Path) -> Path:
    run = Path(run)
    eventos = ler_eventos(run)
    con = sqlite3.connect(f"file:{run / 'estado.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    valor = {r["titulo_id"]: r["valor"] for r in con.execute("SELECT titulo_id, valor FROM titulos")}
    nome_forn = {r["fornecedor_id"]: r["nome_fantasia"] for r in con.execute(
        "SELECT fornecedor_id, nome_fantasia FROM fornecedores_vigente")}
    casos_db = {r["id"]: dict(r) for r in con.execute("SELECT * FROM casos")}

    # ---------------------------------------------------------- fotografias -> tiques + grade do lote
    titulos, idx = [], {}
    lotes: dict[str, list[int]] = {}
    tiques = []
    resumos = {numero_tique(e["tique"]): e for e in eventos if e["tipo"] == "tique_resumo"}
    for p in sorted((run / "tiques").glob("*.json")):
        f = json.loads(p.read_text(encoding="utf-8"))
        n = f["tique"]
        prox = f.get("proximo_lote") or {}
        L = prox.get("lote_remessa") or "-"
        cods = {}
        for t in f.get("titulos_proximo_lote", []):
            tid = t["titulo_id"]
            if tid not in idx:
                idx[tid] = len(titulos)
                q = ", ".join(t.get("quebrados") or [])
                titulos.append([tid, nome_forn.get(t["fornecedor_id"], t["fornecedor_id"]), t["valor"], q])
            lotes.setdefault(L, [])
            if idx[tid] not in lotes[L]:
                lotes[L].append(idx[tid])
            cods[idx[tid]] = codigo(t)
        m = f.get("metricas", {})
        r = resumos.get(n, {})
        ts = f.get("titulos_proximo_lote", [])
        tiques.append({
            "n": n, "hora": f["hora_simulada"], "lote": L, "banco": prox.get("banco"), "fecha": prox.get("fecha_em"),
            "faltam": prox.get("faltam_h"), "cods": cods,
            "no_lote": sum(1 for t in ts if codigo(t) not in ("R", "C")),
            "valor_lote": round(sum(t["valor"] or 0 for t in ts if codigo(t) not in ("R", "C")), 2),
            "fora": sum(1 for t in ts if codigo(t) in ("R", "C")),
            "valor_fora": round(sum(t["valor"] or 0 for t in ts if codigo(t) in ("R", "C")), 2),
            "provado": m.get("pct_provado_so_codigo_lote"), "casos": m.get("casos_abertos", 0),
            "decisoes_pessoas": m.get("decisoes_levadas_a_pessoas", 0), "cards": m.get("cards_humanos", 0),
            "refutou": m.get("critico_refutou", 0), "llm": m.get("llm_chamadas", 0),
            "seg": r.get("segundos"), "llm_tique": int((r.get("llm") or {}).get("chamadas") or 0)})
    for L, ids in lotes.items():
        ids.sort(key=lambda i: -(titulos[i][2] or 0))
    for t in tiques:  # códigos alinhados à lista do lote: string de 1 caractere por título
        t["cods"] = "".join(t["cods"].get(i, "-") for i in lotes.get(t["lote"], []))

    # ---------------------------------------------------------- linha do tempo legível
    feed, passos = [], {}

    def add(e, ic, txt, caso=None, nivel=1):
        n = numero_tique(e["tique"])
        feed.append({"n": n, "ic": ic, "txt": txt, "caso": caso, "nv": nivel})
        if caso:
            passos.setdefault(caso, []).append({"n": n, "ic": ic, "txt": txt})

    for e in eventos:
        t, a = e["tipo"], e.get("args") or {}
        if t == "caso_aberto":
            add(e, "🔎", f"Caso {e['caso_id']} aberto: {nome_forn.get(e.get('fornecedor_id'), e.get('fornecedor_id'))}, "
                         f"{len(e.get('titulos') or [])} título(s), {brl(e.get('valor_em_jogo'))}. Motivo: {_origem(e.get('origem'))}.",
                e["caso_id"])
        elif t == "proposta":
            acoes = ", ".join(x.get("ferramenta", "") for x in (e.get("acoes") or []) if isinstance(x, dict))
            add(e, "🧠", f"Investigador ({e['caso_id']}): {(e.get('hipotese') or '')[:260]}"
                         + (f" → propõe: {acoes}." if acoes else " → manter no lote."), e["caso_id"])
        elif t == "veredito":
            if e.get("reproduziu"):
                add(e, "✅", f"Crítico refez a checagem com as próprias consultas e concordou ({e['caso_id']}).", e["caso_id"], 2)
            else:
                ob = (e.get("objecoes") or [""])[0]
                add(e, "❌", f"Crítico NÃO concordou ({e['caso_id']}): {str(ob)[:220]}", e["caso_id"])
        elif t == "critico_dispensado":
            add(e, "⚡", f"Crítico dispensado ({e['caso_id']}): {e.get('motivo')}.", e["caso_id"], 2)
        elif t == "caso_adiado":
            add(e, "⏳", f"Caso {e['caso_id']} espera: o lote que fecha antes tem prioridade.", e["caso_id"], 2)
        elif t == "ferramenta" and e.get("tipo_ferramenta") == "execucao" and e.get("ator") in (
                "executor", "watchdog", "pontualidade"):
            r = e.get("resultado") or {}
            tid = a.get("titulo_id")
            alvo = f"{tid} ({brl(valor.get(tid))})" if tid else nome_forn.get(a.get("fornecedor_id"), a.get("fornecedor_id") or "")
            caso = next((c for c, d in casos_db.items() if tid and tid in (d["titulo_ids"] or "")), None)
            if r.get("requer_aprovacao_de"):
                add(e, "🛡", f"Guarda da política barrou {e['ferramenta']} em {alvo}: exige "
                             f"{', '.join(r['requer_aprovacao_de'])}. Virou pedido de aprovação.", caso)
            elif r.get("ok"):
                verbo = {"retirar_do_lote": "⏸ Tirou do lote", "cancelar_titulo": "🗑 Cancelou",
                         "bloquear_titulo": "⛔ Bloqueou", "bloquear_fornecedor": "⛔ Bloqueou o fornecedor",
                         "devolver_ao_lote": "↩️ Devolveu ao lote", "reagendar_titulo": "📅 Reagendou",
                         "incluir_no_lote": "↩️ Devolveu ao lote", "solicitar_aprovacao": "✋ Pediu aprovação para",
                         "contatar_fornecedor": "📞 Contatou pelo canal do cadastro",
                         "agendar_verificacao": "🗓 Agendou verificação de"}.get(e["ferramenta"], f"• {e['ferramenta']}")
                quem = {"watchdog": " (regra do prazo)", "pontualidade": " (Pontualidade)"}.get(e["ator"], "")
                add(e, verbo.split(" ")[0], f"{verbo.split(' ', 1)[1]} {alvo}{quem}.", caso, 1 if "lote" in verbo or
                    "Cancelou" in verbo or "Bloqueou" in verbo else 2)
        elif t == "ferramenta" and e.get("ferramenta") == "salvar_detector" and (e.get("resultado") or {}).get("ok"):
            add(e, "🧩", f"Aprendeu um detector novo: {a.get('nome')}.", None, 2)
        elif t == "card_escalada":
            pessoa = (e.get("texto") or "").split(":")[0].lstrip("@") or e.get("pessoa_id")
            add(e, "📨", f"Card para {pessoa}: {e.get('itens')} decisão(ões) que a política reserva a pessoas.")
        elif t == "resposta_fornecedor" and e.get("respondeu"):
            add(e, "✉️", f"Respondeu sozinho a {nome_forn.get(e.get('fornecedor_id'), e.get('fornecedor_id'))} "
                         f"({(e.get('intencao') or '').replace('_', ' ')}), pelo e-mail do cadastro.", None, 2)
        elif t == "nao_responde":
            add(e, "🚫", "Não respondeu a uma mensagem: o remetente não é o contato do cadastro.", None, 2)
        elif t == "lote_fechado":
            L = e.get("lote_remessa")
            fora = con.execute("SELECT count(*), coalesce(sum(valor), 0) FROM titulos WHERE lote_original = ? AND "
                               "titulo_id NOT IN (SELECT titulo_id FROM lote_saida WHERE lote_remessa = ?)", [L, L]).fetchone()
            add(e, "🏦", f"Lote {L} fechou: {e.get('qtd_enviados')} títulos saíram ({brl(e.get('valor_enviado'))}); "
                         f"{fora[0]} ficaram de fora pelo sistema ({brl(fora[1])}).")
        elif t in ("detector_consolidado", "detector_rebaixado"):
            add(e, "🧹", f"Curador: {e.get('motivo')}", None, 2)
        elif t == "execucao_retomada":
            add(e, "🔧", f"Operação: execução retomada ({e.get('motivo')}).", None, 2)

    casos = []
    for cid, d in sorted(casos_db.items()):
        acoes = json.loads(d["acao_escolhida"] or "[]")
        if d["status"] == "decidido":
            desf = "Mantido no lote: era legítimo" if not acoes else "Ação executada"
        else:
            desf = {"aguardando_aprovacao": "Aguardando aprovação de uma pessoa", "inconclusivo": "Inconclusivo",
                    "triado": "Na fila", "em_investigacao": "Em investigação"}.get(d["status"], d["status"])
        n0 = next((p["n"] for p in passos.get(cid, [])), 0)
        casos.append({"id": cid, "n0": n0, "forn": nome_forn.get(d["fornecedor_id"], d["fornecedor_id"]),
                      "valor": d["valor_em_jogo"], "lote": d["lote_remessa"], "prazo": d["prazo"],
                      "origem": _origem(d["origem"]), "p": d["p_problema"], "desfecho": desf,
                      "hipotese": d["hipotese"], "titulos": json.loads(d["titulo_ids"] or "[]"),
                      "acoes": [x.get("ferramenta") for x in acoes], "passos": passos.get(cid, [])})

    resumo = json.loads((run / "resumo.json").read_text(encoding="utf-8")) if (run / "resumo.json").exists() else None
    cur = run.parent / f"{run.name}_curadoria" / "curadoria.json"
    curadoria = json.loads(cur.read_text(encoding="utf-8")) if cur.exists() else None
    if resumo:
        for l in resumo.get("lotes_fechados", []):
            for a in l.get("obrigacoes_atrasadas", []):
                a["forn"] = nome_forn.get(a["obrigacao"]["fornecedor_id"], a["obrigacao"]["fornecedor_id"])
        resumo["casos"].pop("lista", None)
    # ---------------------------------------------------------- cada pagamento ilegítimo evitado, um a um
    quando: dict[str, int] = {}
    for e in eventos:
        if e["tipo"] == "ferramenta" and e.get("tipo_ferramenta") == "execucao" and e.get("ator") in (
                "executor", "watchdog") and (e.get("args") or {}).get("titulo_id"):
            quando.setdefault(e["args"]["titulo_id"], numero_tique(e["tique"]))
    caso_de = {}
    for cid, d in casos_db.items():
        for tid in json.loads(d["titulo_ids"] or "[]"):
            caso_de[tid] = d
    forn_de = {r["titulo_id"]: r["fornecedor_id"] for r in con.execute("SELECT titulo_id, fornecedor_id FROM titulos")}

    def item(tid, tipo, p, lote, motivo_extra=""):
        c = caso_de.get(tid) or {}
        v = valor.get(tid) or 0
        return {"titulo": tid, "forn": nome_forn.get(forn_de.get(tid), forn_de.get(tid)), "valor": v, "lote": lote,
                "tipo": tipo, "p": p, "esperado": round(p * v, 2) if p is not None else 0.0, "caso": c.get("id"),
                "atraso": 0.0,  # cópia de obrigação já paga: segurar não atrasa nada
                "motivo": (c.get("hipotese") or motivo_extra or "")[:400], "n": quando.get(tid, c and next(
                    (pp["n"] for pp in passos.get(c.get("id"), [])), None))}
    evitados = []
    if resumo:
        for l in resumo.get("lotes_fechados", []):
            for cp in l.get("copias_retidas", []):
                evitados.append(item(cp["titulo_id"], "pagamento em dobro", 1.0, l["lote_remessa"],
                                     "a mesma obrigação (fornecedor + NF + parcela) foi paga pela outra via"))
            for a in l.get("obrigacoes_atrasadas", []):
                if a["causa"] == "bug":
                    continue  # retido por erro do código: era legítimo, entra como custo, não como evitado
                alvos = [t["titulo_id"] for t in a["titulos"] if t["alvo_de_acao"]]
                if alvos and a.get("valor_indevido", a["valor"]) > 0:  # uma obrigação = um pagamento possível
                    it = item(alvos[0], "suspeito", a["p_problema_agente"], l["lote_remessa"])
                    it["titulo"] = ", ".join(t["titulo_id"] for t in a["titulos"])
                    it["valor"], it["esperado"] = a.get("valor_indevido", a["valor"]), a["dinheiro_barrado_esperado"]
                    it["atraso"] = a["custo_atraso_se_legitimo"]
                    if a.get("valor_correto_agente") is not None:
                        it["motivo"] = (f"Lançado {brl(a['valor'])}, correto {brl(a['valor_correto_agente'])}: "
                                        f"conta só o excesso. " + it["motivo"])[:400]
                    it["n"] = min((quando[t] for t in alvos if t in quando), default=it["n"])
                    evitados.append(it)
        for f in resumo["lotes_futuros"]["itens"]:
            if f.get("valor_indevido", f["valor"]) <= 0:
                continue  # correção para mais: paga-se MAIS, não é pagamento ilegítimo
            it = item(f["titulo_id"], "lote futuro", f["p_problema_agente"], f["lote"],
                      "elo(s) quebrado(s): " + ", ".join(f.get("quebrados") or []))
            it["titulo"], it["valor"] = ", ".join(f.get("titulos") or [f["titulo_id"]]), f.get("valor_indevido", f["valor"])
            if f.get("valor_correto_agente") is not None:
                it["motivo"] = (f"Lançado {brl(f['valor'])}, correto {brl(f['valor_correto_agente'])}: "
                                f"conta só o excesso. " + it["motivo"])[:400]
            it["esperado"] = f["protegido_esperado"] or 0.0
            it["atraso"] = f["custo_atraso_se_legitimo"]
            it["n"] = min((quando[t] for t in f.get("titulos", []) if t in quando), default=it["n"])
            evitados.append(it)
        resumo["lotes_futuros"]["itens"] = []

    ultima = tiques[-1] if tiques else {}
    detectores = ultima and json.loads((run / "tiques" / f"{ultima['n']:03d}.json").read_text(encoding="utf-8")).get(
        "detectores", [])
    dados = {"run": run.name, "reconstruida": (run / "reconstrucao.json").exists(), "titulos": titulos,
             "lotes": lotes, "tiques": tiques, "feed": feed, "casos": casos, "resumo": resumo, "evitados": evitados,
             "curadoria": curadoria and {k: v for k, v in curadoria.items() if k != "detectores"},
             "detectores": detectores or [], "gerado_em": __import__("time").strftime("%d/%m %H:%M")}
    html = (RAIZ / "app" / "painel.html").read_text(encoding="utf-8")
    html = html.replace("/*__DADOS__*/null", json.dumps(dados, ensure_ascii=False, default=str).replace("</", "<\\/"))
    destino = run / "painel.html"
    destino.write_text(html, encoding="utf-8")
    return destino


if __name__ == "__main__":
    if len(sys.argv) > 1:
        alvo = Path(sys.argv[1])
    else:
        alvo = max((p for p in (RAIZ / "runs").iterdir() if (p / "tiques").exists()), key=lambda p: p.name)
    print(gerar(alvo))
