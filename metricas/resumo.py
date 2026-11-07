"""Resumo de uma execução (9c.5 + 9d.4): `runs/<ts>/resumo.md` e `resumo.json`, os números do pitch.

Sem gabarito: tudo vem da trilha (eventos, estado, fotografias) e das regras de `politicas.csv`. Regras do placar:
- atraso contado POR OBRIGAÇÃO (fornecedor + NF + parcela), nunca por título: um par duplicado é 1 atraso possível;
- atraso por BUG (título retido sem ser alvo de nenhuma ação decidida) separado de atraso por DECISÃO do agente;
- suspeitos com custo esperado = (1 - p) × custo de atraso, com p = probabilidade de problema estimada pelo agente;
- dinheiro barrado rotulado "estimado pelo agente, verificável pela trilha";
- R$ protegido em LOTES FUTUROS separado do lote que já fechou.

    python -m metricas.resumo runs/<ts> [runs/<ts2> ...]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import Counter, defaultdict
from pathlib import Path

from agente.custos import custo_acionamento, custo_atraso
from agente.gravacao import ler_eventos, numero_tique
from env import db

FORA = {"retido", "bloqueado", "cancelado", "aguardando_aprovacao", "aguardando_fornecedor", "reagendado"}
ROTULO_EST = "estimado pelo agente, verificável pela trilha"


def _q(con, sql, p=()):
    return [dict(r) for r in con.execute(sql, p)]


def _j(v, d):
    try:
        return json.loads(v) if v else d
    except (TypeError, json.JSONDecodeError):
        return d


def _alvos_decididos(con, eventos) -> set[str]:
    """Títulos que foram ALVO de uma ação decidida (caso) ou da regra do prazo/pontualidade."""
    alvos: set[str] = set()
    for c in _q(con, "SELECT titulo_ids, acao_escolhida FROM casos"):
        tits = _j(c["titulo_ids"], [])
        for a in _j(c["acao_escolhida"], []):
            p = a.get("parametros") or {}
            if p.get("titulo_id"):
                alvos.add(p["titulo_id"])
            elif p.get("fornecedor_id"):
                alvos.update(tits)
    for e in eventos:
        if e["tipo"] == "ferramenta" and e.get("ator") in ("watchdog", "pontualidade") and e.get("tipo_ferramenta") == "execucao":
            if (e.get("args") or {}).get("titulo_id"):
                alvos.add(e["args"]["titulo_id"])
    return alvos


def resumir(run: Path) -> dict:
    run = Path(run)
    con = sqlite3.connect(f"file:{run / 'estado.sqlite'}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    pol = db.politicas()
    eventos = ler_eventos(run)
    tit = {t["titulo_id"]: t for t in _q(con, "SELECT * FROM titulos")}
    cat = {f["fornecedor_id"]: f["categoria"] for f in _q(con, "SELECT fornecedor_id, categoria FROM fornecedores_vigente")}
    casos = _q(con, "SELECT * FROM casos ORDER BY id")
    caso_de: dict[str, dict] = {}
    for c in casos:
        for t in _j(c["titulo_ids"], []):
            caso_de[t] = c
    alvos = _alvos_decididos(con, eventos)
    # correção de valor decidida pelo agente: o indevido é só o EXCESSO (a parte certa ainda é devida);
    # correção para MAIS (lançamento abaixo da duplicata) não é pagamento ilegítimo
    valor_correto: dict[str, float] = {}
    for c in casos:
        for a in _j(c["acao_escolhida"], []):
            p = a.get("parametros") or {}
            if a.get("ferramenta") == "corrigir_lancamento" and p.get("campo") == "valor" and p.get("titulo_id"):
                try:
                    valor_correto[p["titulo_id"]] = float(p["valor_novo"])
                except (TypeError, ValueError, KeyError):
                    pass

    def indevido(t) -> float:
        v = float(t["valor"] or 0)
        return round(max(0.0, v - valor_correto[t["titulo_id"]]), 2) if t["titulo_id"] in valor_correto else v

    def custo(t):
        return custo_atraso(pol, t["valor"] or 0, cat.get(t["fornecedor_id"]))

    def obrig(t):
        return (t["fornecedor_id"], t["nf_norm"], t["parcela"])

    # ------------------------------------------------------------ lotes fechados
    lotes = []
    for l in _q(con, "SELECT * FROM lotes WHERE fechado_em IS NOT NULL ORDER BY fechado_em"):
        L = l["lote_remessa"]
        saiu = _q(con, "SELECT * FROM lote_saida WHERE lote_remessa = ?", (L,))
        ids_saiu = {s["titulo_id"] for s in saiu}
        obrig_paga = {obrig(tit[s["titulo_id"]]) for s in saiu if s["titulo_id"] in tit}
        retidos = [t for t in tit.values() if t["lote_original"] == L and t["titulo_id"] not in ids_saiu]
        copias, por_obrig = [], defaultdict(list)
        for t in retidos:
            (copias if obrig(t) in obrig_paga else por_obrig[obrig(t)]).append(t)
        atrasos = []
        for ob, ts in por_obrig.items():
            colaterais = [t for t in ts if t["titulo_id"] not in alvos]
            c = caso_de.get(ts[0]["titulo_id"]) or {}
            p = c.get("p_problema")
            ref = max(ts, key=lambda t: t["valor"] or 0)
            ca = custo(ref)
            if colaterais:
                causa = "bug"
            elif any(t["estado"] == "aguardando_aprovacao" for t in ts):
                causa = "decisão (aguardando aprovação humana)"
            else:
                causa = "decisão do agente"
            atrasos.append({
                "obrigacao": {"fornecedor_id": ob[0], "nf": ob[1], "parcela": ob[2]},
                "titulos": [{"titulo_id": t["titulo_id"], "valor": t["valor"], "estado": t["estado"],
                             "alvo_de_acao": t["titulo_id"] in alvos} for t in ts],
                "valor": ref["valor"], "categoria": cat.get(ref["fornecedor_id"]), "causa": causa,
                "caso_id": c.get("id"), "p_problema_agente": p,
                "custo_atraso_se_legitimo": ca,
                "custo_esperado": ca if causa == "bug" or p is None else round((1 - float(p)) * ca, 2),
                "valor_indevido": indevido(ref), "valor_correto_agente": valor_correto.get(ref["titulo_id"]),
                "dinheiro_barrado_esperado": 0.0 if causa == "bug" or p is None else round(float(p) * indevido(ref), 2),
                "hipotese": (c.get("hipotese") or "")[:300]})
        bug = [a for a in atrasos if a["causa"] == "bug"]
        dec = [a for a in atrasos if a["causa"] != "bug"]
        duplo_evitado = round(sum(t["valor"] or 0 for t in copias), 2)
        barrado = round(sum(a["dinheiro_barrado_esperado"] for a in dec), 2)
        custo_bug = round(sum(a["custo_atraso_se_legitimo"] for a in bug), 2)
        custo_dec = round(sum(a["custo_esperado"] for a in dec), 2)
        lotes.append({
            "lote_remessa": L, "assinado_por": l["assinado_por"],
            "saiu": {"qtd": len(saiu), "valor": round(sum(s["valor"] or 0 for s in saiu), 2)},
            "retidos": {"qtd": len(retidos), "valor": round(sum(t["valor"] or 0 for t in retidos), 2)},
            "copias_retidas": [{"titulo_id": t["titulo_id"], "valor": t["valor"], "estado": t["estado"]} for t in copias],
            "pagamento_em_dobro_evitado": duplo_evitado,
            "obrigacoes_atrasadas": atrasos,
            "placar": {
                "sem_agente": {"pago_em_dobro": duplo_evitado, "pago_indevido_esperado": barrado,
                               "total": round(duplo_evitado + barrado, 2)},
                "com_agente": {"custo_atraso_bug": custo_bug, "custo_atraso_decisao_esperado": custo_dec,
                               "custo_atraso_decisao_se_todos_legitimos": round(sum(a["custo_atraso_se_legitimo"]
                                                                                    for a in dec), 2)},
                "ganho_liquido_estimado": round(duplo_evitado + barrado - custo_bug - custo_dec, 2),
                "rotulo": ROTULO_EST}})

    # ------------------------------------------------------------ lotes futuros (até o fim da execução)
    fechados = {l["lote_remessa"] for l in lotes}
    fut_ob: dict[tuple, list] = defaultdict(list)  # POR OBRIGAÇÃO: um par duplicado segurado inteiro conta uma vez
    for t in tit.values():
        if t["lote_original"] in fechados or not t["lote_original"] or t["estado"] not in FORA:
            continue
        fut_ob[obrig(t)].append(t)
    futuros = []
    for ob, ts in fut_ob.items():
        ref = max(ts, key=lambda t: t["valor"] or 0)
        c = next((caso_de[t["titulo_id"]] for t in ts if t["titulo_id"] in caso_de), {})
        p = c.get("p_problema")
        cj = _j(ref["cadeia_json"], {})
        futuros.append({"titulo_id": ref["titulo_id"], "titulos": [t["titulo_id"] for t in ts],
                        "fornecedor_id": ref["fornecedor_id"], "lote": ref["lote_original"],
                        "valor": ref["valor"], "estado": ref["estado"], "quebrados": cj.get("quebrados", []),
                        "valor_indevido": indevido(ref), "valor_correto_agente": valor_correto.get(ref["titulo_id"]),
                        "caso_id": c.get("id"), "p_problema_agente": p,
                        "protegido_esperado": round(float(p) * indevido(ref), 2) if p is not None else None,
                        "custo_atraso_se_legitimo": custo(ref), "evidencias": _j(c.get("evidencias"), [])[:6]})
    forn_bloq = _q(con, "SELECT fornecedor_id, motivo_bloqueio FROM fornecedores_vigente WHERE bloqueado = 1")

    # ------------------------------------------------------------ casos, Crítico, guarda, pessoas
    def desfecho(c):
        if c["status"] == "decidido":
            return "mantido no lote após investigação" if not _j(c["acao_escolhida"], []) else "ação executada"
        return {"aguardando_aprovacao": "aguardando aprovação humana", "inconclusivo": "inconclusivo (Crítico refutou 2x)",
                "triado": "não investigado até o fim (fila)", "em_investigacao": "em investigação"}.get(c["status"], c["status"])
    tipos = Counter(e["tipo"] for e in eventos)
    cards = _q(con, "SELECT * FROM cards")
    decisoes = sum(len(_j(c["itens"], [])) for c in cards)
    em_card = set()
    for c in cards:
        for it in _j(c["itens"], []):
            em_card.update(_j(it.get("titulos"), []) if isinstance(it, dict) else [])
    n_tit = len([t for t in tit.values() if t["lote_original"] in fechados])
    sem_pessoa = len([t for t in tit.values() if t["lote_original"] in fechados and t["titulo_id"] not in em_card])
    acoes = Counter(e.get("ferramenta") for e in eventos if e["tipo"] == "ferramenta" and e.get("tipo_ferramenta") == "execucao")
    barradas = sum(1 for e in eventos if e["tipo"] == "ferramenta" and isinstance(e.get("resultado"), dict)
                   and e["resultado"].get("requer_aprovacao_de"))

    # detectores antes/depois da curadoria
    dets = _q(con, "SELECT id, ativo FROM detectores")
    cur = run.parent / f"{run.name}_curadoria" / "curadoria.json"
    if cur.exists():
        cj = json.loads(cur.read_text(encoding="utf-8"))
        detectores = {"criados": cj["detectores_antes"], "ativos_depois_da_curadoria": cj["ativos_depois"],
                      "consolidados": len(cj["consolidados"]), "rebaixados": len(cj["rebaixados"]),
                      "curadoria": "pós-execução (pasta irmã _curadoria)"}
    else:
        detectores = {"criados": len(dets), "ativos_depois_da_curadoria": sum(1 for d in dets if d["ativo"]),
                      "consolidados": tipos["detector_consolidado"], "rebaixados": tipos["detector_rebaixado"],
                      "curadoria": "nativa (Curador às 18h)"}

    # ------------------------------------------------------------ tempos
    res_t = [e for e in eventos if e["tipo"] == "tique_resumo"]
    seg = [e.get("segundos") or 0 for e in res_t]
    por_papel = defaultdict(lambda: {"chamadas": 0, "segundos": 0.0})
    por_modelo = defaultdict(lambda: {"chamadas": 0, "segundos": 0.0})
    por_t = defaultdict(lambda: defaultdict(float))
    fora_t = defaultdict(float)
    for e in eventos:
        if e["tipo"] != "llm_chamada":
            continue
        s = e.get("latencia_s") or 0
        por_papel[e["ator"]]["chamadas"] += 1
        por_papel[e["ator"]]["segundos"] = round(por_papel[e["ator"]]["segundos"] + s, 1)
        por_modelo[e.get("modelo")]["chamadas"] += 1
        por_modelo[e.get("modelo")]["segundos"] = round(por_modelo[e.get("modelo")]["segundos"] + s, 1)
        n = numero_tique(e["tique"])
        if str(e.get("contexto") or "").startswith("CASO"):
            por_t[n][e["contexto"]] += s
        else:
            fora_t[n] += s
    N = 4
    proj = sum(max(max(por_t[n].values(), default=0), sum(por_t[n].values()) / N) + fora_t[n] / N
               for n in {numero_tique(e["tique"]) for e in res_t})
    tempos = {"tiques": len(res_t), "segundos_total": round(sum(seg), 1), "tique_medio_s": round(sum(seg) / len(seg), 1) if seg else None,
              "tique_max_s": max(seg) if seg else None,
              "pct_da_hora_no_tique_max": round(100 * max(seg) / 3600, 1) if seg else None,
              "tiques_sem_llm": sum(1 for e in res_t if not (e.get("llm") or {}).get("chamadas")),
              "llm_chamadas": tipos["llm_chamada"], "llm_falhas": tipos["llm_falha"],
              "llm_saidas_invalidas": tipos["llm_saida_invalida"],
              "por_papel": dict(por_papel), "por_modelo": dict(por_modelo),
              "projecao_paralela_s": round(proj, 1),
              "projecao_premissa": f"{N} chamadas simultâneas sem fila; max(caso mais longo, soma dos casos/{N}) + resto/{N}"}

    cfg = next((e for e in eventos if e["tipo"] == "configuracao"), {})
    out = {
        "execucao": run.name, "configuracao": {k: cfg.get(k) for k in ("modelos", "pools", "orcamento", "paralelo")},
        "ate": res_t[-1]["agora"] if res_t else None,
        "lotes_fechados": lotes,
        "lotes_futuros": {"titulos_fora_do_lote": sum(len(f["titulos"]) for f in futuros),
                          "obrigacoes_fora_do_lote": len(futuros),
                          "sem_probabilidade_do_agente": sum(1 for f in futuros if f["p_problema_agente"] is None),
                          "valor_fora_do_lote": round(sum(f["valor"] or 0 for f in futuros), 2),
                          "valor_indevido_barrado": round(sum(f["valor_indevido"] for f in futuros), 2),
                          "correcoes_para_mais": sum(1 for f in futuros if f["valor_correto_agente"] is not None
                                                     and f["valor_indevido"] == 0),
                          "protegido_esperado": round(sum(f["protegido_esperado"] or 0 for f in futuros), 2),
                          "custo_atraso_se_todos_legitimos": round(sum(f["custo_atraso_se_legitimo"] for f in futuros), 2),
                          "fornecedores_bloqueados": forn_bloq, "rotulo": ROTULO_EST, "itens": futuros},
        "casos": {"total": len(casos), "por_origem": dict(Counter((c["origem"] or "?").split(":")[0] for c in casos)),
                  "por_desfecho": dict(Counter(desfecho(c) for c in casos)),
                  "por_lote": dict(Counter(c["lote_remessa"] or "sem lote" for c in casos)),
                  "adiados_pelo_prazo": tipos["caso_adiado"],
                  "lista": [{"id": c["id"], "origem": c["origem"], "lote": c["lote_remessa"], "desfecho": desfecho(c),
                             "valor_em_jogo": c["valor_em_jogo"], "p_problema": c["p_problema"]} for c in casos]},
        "critico": {"vereditos": tipos["veredito"], "refutacoes": tipos["critico_refutou"],
                    "dispensas": tipos["critico_dispensado"]},
        "guarda": {"acoes_barradas_viraram_pedido_de_aprovacao": barradas, "acoes_executadas": dict(acoes)},
        "pessoas": {"cards": len(cards), "decisoes_levadas": decisoes,
                    "custo_maximo_se_todas_desnecessarias": round(decisoes * custo_acionamento(pol), 2),
                    "pct_titulos_dos_lotes_fechados_sem_pessoa": round(100 * sem_pessoa / n_tit, 1) if n_tit else None},
        "relacionamento": {"mensagens_tratadas": tipos["resposta_fornecedor"] + tipos["nao_responde"],
                           "respostas_enviadas":sum(1 for e in eventos if e["tipo"] == "resposta_fornecedor" and e.get("respondeu")),
                           "remetente_fora_do_cadastro": tipos["nao_responde"],
                           "encaminhamentos_adiados_sem_fato": tipos["encaminhamento_adiado"]},
        "detectores": detectores,
        "tempos": tempos,
    }
    (run / "resumo.json").write_text(json.dumps(out, ensure_ascii=False, indent=2, default=str), encoding="utf-8")
    (run / "resumo.md").write_text(markdown(out), encoding="utf-8")
    return out


def brl(v) -> str:
    return "R$ " + f"{float(v or 0):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")


def markdown(r: dict) -> str:
    L = [f"# Resumo da execução {r['execucao']}", "", f"Até {r['ate']}. Valores de dinheiro barrado: *{ROTULO_EST}*.", ""]
    for l in r["lotes_fechados"]:
        p = l["placar"]
        L += [f"## Lote {l['lote_remessa']}", "",
              f"- Saiu: **{l['saiu']['qtd']} títulos, {brl(l['saiu']['valor'])}**; ficaram fora: {l['retidos']['qtd']} "
              f"({brl(l['retidos']['valor'])}).",
              f"- Cópias retidas com a obrigação paga pela outra via: {len(l['copias_retidas'])} → "
              f"**{brl(l['pagamento_em_dobro_evitado'])} em dobro evitados**.",
              f"- Obrigações atrasadas (contadas por fornecedor + NF + parcela): {len(l['obrigacoes_atrasadas'])}", ""]
        L += ["| Obrigação | Valor | Causa | p(problema) do agente | Custo de atraso se legítimo | Custo esperado |",
              "|---|---|---|---|---|---|"]
        for a in l["obrigacoes_atrasadas"]:
            o = a["obrigacao"]
            L.append(f"| {o['fornecedor_id']} NF {o['nf']} parc. {o['parcela']} ({len(a['titulos'])} tít.) | {brl(a['valor'])} | "
                     f"{a['causa']} | {a['p_problema_agente'] if a['p_problema_agente'] is not None else '-'} | "
                     f"{brl(a['custo_atraso_se_legitimo'])} | {brl(a['custo_esperado'])} |")
        L += ["", "**Placar do lote**", "",
              f"- Sem agente (o lote sai como está): {brl(p['sem_agente']['pago_em_dobro'])} pagos em dobro + "
              f"{brl(p['sem_agente']['pago_indevido_esperado'])} de pagamento indevido esperado = **{brl(p['sem_agente']['total'])}**.",
              f"- Com agente: atraso por **bug** {brl(p['com_agente']['custo_atraso_bug'])}; atraso por **decisão** "
              f"{brl(p['com_agente']['custo_atraso_decisao_esperado'])} esperado "
              f"(até {brl(p['com_agente']['custo_atraso_decisao_se_todos_legitimos'])} se todos os retidos forem legítimos).",
              f"- Ganho líquido estimado: **{brl(p['ganho_liquido_estimado'])}**.", ""]
    f = r["lotes_futuros"]
    L += ["## Lotes futuros (separado do lote que fechou)", "",
          f"- {f['obrigacoes_fora_do_lote']} pagamentos (obrigações; {f['titulos_fora_do_lote']} títulos) de lotes ainda "
          f"abertos já estão fora do lote ({brl(f['valor_fora_do_lote'])}); {f['sem_probabilidade_do_agente']} sem "
          f"probabilidade estimada pelo agente (contam zero no esperado).",
          f"- Valor indevido barrado: {brl(f['valor_indevido_barrado'])} (em correção de valor conta só o excesso; "
          f"{f['correcoes_para_mais']} correção(ões) para mais não contam como pagamento ilegítimo).",
          f"- Protegido esperado (p × valor): **{brl(f['protegido_esperado'])}**; custo de atraso se todos forem legítimos: "
          f"{brl(f['custo_atraso_se_todos_legitimos'])}.",
          f"- Fornecedores bloqueados: {len(f['fornecedores_bloqueados'])}.", ""]
    c = r["casos"]
    L += [f"## Casos ({c['total']})", "", f"- Por origem: {c['por_origem']}", f"- Por desfecho: {c['por_desfecho']}",
          f"- Por lote: {c['por_lote']}", f"- Adiados pelo prazo: {c['adiados_pelo_prazo']}", "",
          "## Crítico, guarda e pessoas", "",
          f"- Crítico: {r['critico']['vereditos']} vereditos, {r['critico']['refutacoes']} refutações, "
          f"{r['critico']['dispensas']} dispensas (regra de custo).",
          f"- Guarda: {r['guarda']['acoes_barradas_viraram_pedido_de_aprovacao']} ações barradas viraram pedido de aprovação.",
          f"- Pessoas: {r['pessoas']['cards']} cards, {r['pessoas']['decisoes_levadas']} decisões "
          f"(no máximo {brl(r['pessoas']['custo_maximo_se_todas_desnecessarias'])} se todas fossem desnecessárias); "
          f"{r['pessoas']['pct_titulos_dos_lotes_fechados_sem_pessoa']}% dos títulos dos lotes fechados sem pessoa.",
          f"- Relacionamento: {r['relacionamento']['mensagens_tratadas']} mensagens de fornecedor tratadas, "
          f"{r['relacionamento']['respostas_enviadas']} respostas enviadas sozinho; "
          f"{r['relacionamento']['remetente_fora_do_cadastro']} remetentes fora do cadastro sem resposta.",
          f"- Detectores: {r['detectores']['criados']} criados → {r['detectores']['ativos_depois_da_curadoria']} ativos "
          f"({r['detectores']['consolidados']} consolidados, {r['detectores']['rebaixados']} rebaixados; {r['detectores']['curadoria']}).", ""]
    t = r["tempos"]
    L += ["## Tempos", "",
          f"- {t['tiques']} tiques em {t['segundos_total'] / 60:.0f} min de relógio; tique médio {t['tique_medio_s']} s, "
          f"máximo {t['tique_max_s']} s ({t['pct_da_hora_no_tique_max']}% da hora simulada).",
          f"- {t['tiques_sem_llm']} tiques sem LLM; {t['llm_chamadas']} chamadas, {t['llm_falhas']} falhas, "
          f"{t['llm_saidas_invalidas']} saídas inválidas.",
          f"- Projeção com paralelização: {t['projecao_paralela_s'] / 60:.0f} min ({t['projecao_premissa']}).", "",
          "| Papel | Chamadas | Segundos |", "|---|---|---|"]
    for k, v in sorted(t["por_papel"].items(), key=lambda x: -x[1]["segundos"]):
        L.append(f"| {k} | {v['chamadas']} | {v['segundos']:.0f} |")
    L += ["", "| Modelo | Chamadas | Segundos |", "|---|---|---|"]
    for k, v in sorted(t["por_modelo"].items(), key=lambda x: -x[1]["segundos"]):
        L.append(f"| {k} | {v['chamadas']} | {v['segundos']:.0f} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8")
    for a in sys.argv[1:]:
        r = resumir(Path(a))
        print(markdown(r))
