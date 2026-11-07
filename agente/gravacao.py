"""Gravação da execução para a interface (seção F3d): o Streamlit só LÊ `runs/<ts>/`, nunca recalcula.

- `tiques/<NNN>.json`: fotografia do estado ao fim de cada tique (escrita atômica).
- `llm/<NNN>.jsonl`: cada chamada de LLM do tique (papel, modelo, latência, tokens, mensagens resumidas, resposta).
- `lotes/<lote>.json`: no fechamento, o que saiu, o que ficou e por quê, e o pós-lote.

A mesma função `fotografia` serve à gravação nativa (estado vivo) e a `scripts/reconstruir_tiques.py` (estado final
+ eventos). O estado de cada título NO TIQUE vem da tabela `transicoes` (histórico), então vale para os dois modos; a
cadeia (elos) só existe como foto viva na gravação nativa: na reconstrução ela é a final e isso fica em `faltaram`.
"""
from __future__ import annotations

import json
import os
import threading
from collections import Counter, defaultdict
from datetime import datetime
from pathlib import Path

INICIO = datetime(2026, 11, 2, 8, 0)
RETIDOS = {"retido", "bloqueado", "aguardando_fornecedor", "aguardando_aprovacao", "reagendado", "cancelado"}
LIMITE_MSG = 4000


def numero_tique(agora: str) -> int:
    return int((datetime.fromisoformat(agora[:19]) - INICIO).total_seconds() // 3600)


def escrever_atomico(caminho: Path, texto: str) -> None:
    caminho.parent.mkdir(parents=True, exist_ok=True)
    tmp = caminho.with_suffix(caminho.suffix + ".tmp")
    tmp.write_text(texto, encoding="utf-8")
    os.replace(tmp, caminho)


def ler_eventos(run_dir: Path) -> list[dict]:
    p = Path(run_dir) / "eventos.jsonl"
    if not p.exists():
        return []
    out = []
    for linha in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(linha))
        except json.JSONDecodeError:
            pass  # linha sendo escrita (execução em andamento)
    return out


def _json(v, padrao):
    try:
        return json.loads(v) if isinstance(v, str) and v else (v if v is not None else padrao)
    except json.JSONDecodeError:
        return padrao


# ---------------------------------------------------------------------- estado no tique
def estados_em(est, agora: str) -> dict[str, str]:
    """Estado de cada título no instante `agora`, pelo histórico de transições."""
    atual = {r["titulo_id"]: r["estado"] for r in est.q("SELECT titulo_id, estado FROM titulos")}
    antes: dict[str, str] = {}
    primeiro_depois: dict[str, str] = {}
    for r in est.q("SELECT titulo_id, de, para, quando FROM transicoes ORDER BY id"):
        if r["quando"] <= agora:
            antes[r["titulo_id"]] = r["para"]
        else:
            primeiro_depois.setdefault(r["titulo_id"], r["de"])
    for tid in atual:
        if tid in antes:
            atual[tid] = antes[tid]
        elif tid in primeiro_depois:
            atual[tid] = primeiro_depois[tid]
    return atual


def _proximo_lote(est, agora: str) -> dict | None:
    for l in est.q("SELECT * FROM lotes ORDER BY data_pagamento, horario_fechamento"):
        fecha = f"{l['data_pagamento']} {l['horario_fechamento']}"
        if fecha[:16] > agora[:16]:
            falta = (datetime.fromisoformat(fecha[:16]) - datetime.fromisoformat(agora[:16])).total_seconds() / 3600
            return {"lote_remessa": l["lote_remessa"], "banco": l["banco_pagador"], "fecha_em": fecha[:16],
                    "faltam_h": round(falta, 1)}
    return None


def _caso_por_titulo(casos: list[dict]) -> dict[str, str]:
    m = {}
    for c in casos:
        for t in _json(c["titulo_ids"], []):
            m[t] = c["id"]
    return m


def _elos_curto(cj: dict) -> list[dict]:
    return [{"elo": e.get("elo"), "nome": e.get("nome"), "status": e.get("status"),
             "explicacao": (e.get("explicacao") or "")[:300], "evidencias": (e.get("evidencias") or [])[:8]}
            for e in cj.get("elos", [])]


def metricas(eventos: list[dict], est, estados: dict[str, str], agora: str) -> dict:
    """Métricas acumuladas até `agora` (só eventos com tique <= agora)."""
    ev = [e for e in eventos if (e.get("tique") or "") <= agora]
    tipos = Counter(e["tipo"] for e in ev)
    por_papel: dict[str, dict] = defaultdict(lambda: {"chamadas": 0, "segundos": 0.0, "tokens": 0})
    por_modelo: dict[str, dict] = defaultdict(lambda: {"chamadas": 0, "segundos": 0.0})
    for e in ev:
        if e["tipo"] == "llm_chamada":
            p = por_papel[e.get("ator") or e.get("papel")]
            p["chamadas"] += 1
            p["segundos"] = round(p["segundos"] + (e.get("latencia_s") or 0), 2)
            p["tokens"] += e.get("tokens") or 0
            m = por_modelo[e.get("modelo")]
            m["chamadas"] += 1
            m["segundos"] = round(m["segundos"] + (e.get("latencia_s") or 0), 2)
    tit = {r["titulo_id"]: r for r in est.q("SELECT titulo_id, valor, cadeia_json, lote_original, lote_remessa "
                                             "FROM titulos")}
    retido_motivo: dict[str, dict] = defaultdict(lambda: {"qtd": 0, "valor": 0.0})
    for tid, s in estados.items():
        if s in RETIDOS and tid in tit:
            cj = _json(tit[tid]["cadeia_json"], {})
            motivo = ",".join(cj.get("quebrados", [])) or s
            retido_motivo[motivo]["qtd"] += 1
            retido_motivo[motivo]["valor"] = round(retido_motivo[motivo]["valor"] + (tit[tid]["valor"] or 0), 2)
    acoes = Counter(e.get("ferramenta") for e in ev if e["tipo"] == "ferramenta" and e.get("tipo_ferramenta") == "execucao")
    devolvidos = sum(1 for e in ev if e["tipo"] == "ferramenta" and e.get("ator") == "pontualidade"
                     and e.get("ferramenta") in ("devolver_ao_lote", "reagendar_titulo", "incluir_no_lote"))
    cards = [e for e in ev if e["tipo"] == "card_escalada"]
    return {
        "retido_por_motivo": dict(retido_motivo),
        "titulos_devolvidos_pela_pontualidade": devolvidos,
        "cards_humanos": len(cards), "decisoes_levadas_a_pessoas": sum(e.get("itens") or 0 for e in cards),
        "llm_por_papel": dict(por_papel), "llm_por_modelo": dict(por_modelo),
        "llm_chamadas": tipos["llm_chamada"], "llm_falhas": tipos["llm_falha"],
        "casos_abertos": tipos["caso_aberto"], "casos_adiados": tipos["caso_adiado"],
        "propostas": tipos["proposta"], "vereditos": tipos["veredito"],
        "critico_refutou": tipos["critico_refutou"], "critico_dispensado": tipos["critico_dispensado"],
        "citacoes_barradas": tipos["citacao_barrada"],
        "acoes_executadas": dict(acoes),
        "pedidos_de_aprovacao": sum(1 for e in ev if e["tipo"] == "ferramenta" and e.get("ator") == "executor"
                                    and isinstance(e.get("resultado"), dict) and e["resultado"].get("requer")),
        "respostas_a_fornecedor": sum(1 for e in ev if e["tipo"] == "resposta_fornecedor" and e.get("respondeu")),
        "tiques_sem_llm": sum(1 for e in ev if e["tipo"] == "tique_resumo"
                              and not (e.get("llm") or {}).get("chamadas")),
        "tiques": tipos["tique_resumo"],
    }


def fotografia(est, eventos: list[dict], agora: str, reconstrucao: bool = False) -> dict:
    estados = estados_em(est, agora)
    casos = [c for c in est.q("SELECT * FROM casos ORDER BY id") if (c["criado_em"] or "") <= agora]
    caso_de = _caso_por_titulo(casos)
    prox = _proximo_lote(est, agora)
    titulos_lote = []
    if prox:
        lote = prox["lote_remessa"]
        for t in est.q("SELECT titulo_id, fornecedor_id, valor, vencimento, forma_pagamento, cadeia_resultado, "
                       "cadeia_json FROM titulos WHERE lote_remessa = ? OR lote_original = ? ORDER BY valor DESC",
                       [lote, lote]):
            cj = _json(t["cadeia_json"], {})
            titulos_lote.append({
                "titulo_id": t["titulo_id"], "fornecedor_id": t["fornecedor_id"], "valor": t["valor"],
                "vencimento": t["vencimento"], "forma": t["forma_pagamento"], "estado": estados.get(t["titulo_id"]),
                "resultado": t["cadeia_resultado"], "quebrados": cj.get("quebrados", []),
                "inconclusivos": cj.get("inconclusivos", []), "elos": _elos_curto(cj),
                "caso_id": caso_de.get(t["titulo_id"])})
    n = len(titulos_lote)
    so_codigo = sum(1 for t in titulos_lote if t["resultado"] == "PROVADO" and not t["caso_id"])
    cards = [c for c in est.q("SELECT * FROM cards ORDER BY id") if (c["criado_em"] or "") <= agora]
    itens_card = set()
    for c in cards:
        for it in _json(c["itens"], []):
            for tid in _json(it.get("titulos"), []) if isinstance(it, dict) else []:
                itens_card.add(tid)
    sem_pessoa = sum(1 for t in titulos_lote if t["titulo_id"] not in itens_card)
    caso_atual = []
    for c in casos:
        st = c["status"]
        if reconstrucao and (c["atualizado_em"] or "") > agora:
            st = "em_andamento"  # status intermediário não foi gravado na execução antiga
        caso_atual.append({"id": c["id"], "mesa": c["mesa"] or "integridade", "status": st,
                           "fornecedor_id": c["fornecedor_id"], "titulos": _json(c["titulo_ids"], []),
                           "valor_em_jogo": c["valor_em_jogo"], "prazo": c["prazo"], "origem": c["origem"],
                           "p_problema": c["p_problema"], "desfecho": c["desfecho"] if st != "em_andamento" else None})
    try:
        saude = est.q("SELECT * FROM saude_cadeia WHERE quando = (SELECT max(quando) FROM saude_cadeia WHERE quando <= ?)",
                      [agora])
    except Exception:
        saude = []
    m = metricas(eventos, est, estados, agora)
    m["pct_provado_so_codigo_lote"] = round(100 * so_codigo / n, 1) if n else None
    m["pct_sem_pessoa_lote"] = round(100 * sem_pessoa / n, 1) if n else None
    foto = {
        "tique": numero_tique(agora), "hora_simulada": agora, "proximo_lote": prox,
        "titulos_proximo_lote": titulos_lote,
        "casos": caso_atual,
        "cards": [{"id": c["id"], "pessoa_id": c["pessoa_id"], "lote_remessa": c["lote_remessa"],
                   "itens": _json(c["itens"], []), "texto": c["texto"], "criado_em": c["criado_em"],
                   "status": c["status"]} for c in cards],
        "detectores": [dict(d) for d in est.q("SELECT id, nome, descricao, severidade, criado_no_tique, caso_origem, "
                                               "n_alertas, n_confirmados, n_falsos, ativo, motivo_status FROM detectores "
                                               "WHERE criado_no_tique <= ? ORDER BY id", [agora])],
        "licoes": [dict(r) for r in est.q("SELECT * FROM licoes WHERE quando <= ? ORDER BY quando", [agora])],
        "saude_cadeia": [dict(r) for r in saude],
        "metricas": m,
        "faltaram": [],
    }
    if reconstrucao:
        foto["faltaram"] = [
            "cadeia por tique (elos e resultado são os do estado FINAL da execução)",
            "status intermediário dos casos (casos ainda não fechados no tique aparecem como 'em_andamento')",
            "detectores: ativo/precisão são os finais",
            "mensagens enviadas ao LLM e respostas completas (llm/NNN.jsonl só com papel, modelo, latência, tokens)",
        ]
    return foto


def foto_lote(est, eventos: list[dict], lote: str) -> dict:
    l = est.lote(lote)
    saiu = [dict(r) for r in est.q("SELECT * FROM lote_saida WHERE lote_remessa = ? ORDER BY valor DESC", [lote])]
    ids_saiu = {r["titulo_id"] for r in saiu}
    casos = est.q("SELECT * FROM casos")
    caso_de = _caso_por_titulo(casos)
    hipotese = {c["id"]: c["hipotese"] for c in casos}
    retidos = []
    for t in est.q("SELECT titulo_id, fornecedor_id, valor, estado, cadeia_resultado, cadeia_json, motivo_bloqueio "
                   "FROM titulos WHERE lote_original = ? OR lote_remessa = ?", [lote, lote]):
        if t["titulo_id"] in ids_saiu:
            continue
        cj = _json(t["cadeia_json"], {})
        cid = caso_de.get(t["titulo_id"])
        retidos.append({"titulo_id": t["titulo_id"], "fornecedor_id": t["fornecedor_id"], "valor": t["valor"],
                        "estado": t["estado"], "resultado": t["cadeia_resultado"],
                        "quebrados": cj.get("quebrados", []), "caso_id": cid,
                        "motivo": t["motivo_bloqueio"] or (hipotese.get(cid) or "")[:400]})
    fech = next((e for e in eventos if e["tipo"] == "lote_fechado" and e.get("lote_remessa") == lote), {})
    return {"lote_remessa": lote, "lote": dict(l) if l else None, "fechamento": fech, "saiu": saiu,
            "retidos": retidos, "qtd_saiu": len(saiu), "valor_saiu": round(sum(r["valor"] or 0 for r in saiu), 2),
            "qtd_retidos": len(retidos), "valor_retido": round(sum(r["valor"] or 0 for r in retidos), 2)}


# ---------------------------------------------------------------------- gravação nativa
class Gravador:
    def __init__(self, amb):
        self.amb = amb
        self.dir = Path(amb.run_dir)
        self._buf: list[dict] = []
        self._lock = threading.Lock()

    def chamada_llm(self, registro: dict) -> None:
        msgs = []
        for m in registro.get("mensagens") or []:
            c = m.get("content") if isinstance(m, dict) else str(m)
            c = c if isinstance(c, str) else json.dumps(c, ensure_ascii=False, default=str)
            if len(c) > LIMITE_MSG:
                c = c[:LIMITE_MSG // 2] + f"\n…[{len(c) - LIMITE_MSG} caracteres resumidos]…\n" + c[-LIMITE_MSG // 2:]
            item = {"role": m.get("role") if isinstance(m, dict) else "?", "content": c}
            if isinstance(m, dict) and m.get("tool_calls"):
                item["tool_calls"] = [tc["function"]["name"] for tc in m["tool_calls"] if isinstance(tc, dict)]
            msgs.append(item)
        with self._lock:
            self._buf.append({**{k: v for k, v in registro.items() if k != "mensagens"}, "mensagens": msgs})

    def fim_do_tique(self) -> None:
        est = self.amb.est
        agora = est.agora_txt
        n = numero_tique(agora)
        with self._lock:
            buf, self._buf = self._buf, []
        escrever_atomico(self.dir / "llm" / f"{n:03d}.jsonl",
                         "".join(json.dumps(r, ensure_ascii=False, default=str) + "\n" for r in buf))
        eventos = ler_eventos(self.dir)
        escrever_atomico(self.dir / "tiques" / f"{n:03d}.json",
                         json.dumps(fotografia(est, eventos, agora), ensure_ascii=False, default=str))
        for l in est.q("SELECT lote_remessa FROM lotes WHERE fechado_em IS NOT NULL"):
            escrever_atomico(self.dir / "lotes" / f"{l['lote_remessa']}.json",
                             json.dumps(foto_lote(est, eventos, l["lote_remessa"]), ensure_ascii=False, default=str))
