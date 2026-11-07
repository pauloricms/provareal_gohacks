"""Painel do time de agentes de contas a pagar (F5). MODO REPLAY: só LÊ `runs/<ts>/` (formato em runs/SCHEMA.md).
Não chama LLM, não roda a cadeia, não lê os CSVs. Funciona também sobre uma execução em andamento.

    streamlit run app/streamlit_app.py
"""
from __future__ import annotations

import json
import sqlite3
import sys
import time
from collections import Counter, defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import pandas as pd
import streamlit as st

RAIZ = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(RAIZ))
from agente.gravacao import INICIO, numero_tique  # noqa: E402

RUNS = RAIZ / "runs"
SEMAFORO = {"PROVADO": "🟢", "DUVIDOSO": "🟡", "QUEBRADO": "🔴"}
SAIU = {"certificado", "agendado", "enviado", "retorno_banco", "conciliado", "baixado", "cadeia_avaliada", "lancado"}
STATUS_ELO = {"OK": "✅", "QUEBRADO": "❌", "INCONCLUSIVO": "❔", "NAO_SE_APLICA": "➖", "PENDENTE_LLM": "⏳"}

st.set_page_config(page_title="Contas a pagar: time de agentes", page_icon="🧾", layout="wide")


# ---------------------------------------------------------------------- leitura (com cache por mtime)
def execucoes() -> list[Path]:
    return sorted([p for p in RUNS.iterdir() if p.is_dir() and (p / "tiques").exists()], reverse=True)


@st.cache_data(show_spinner=False)
def _ler_eventos(caminho: str, _mtime: float) -> list[dict]:
    out = []
    with open(caminho, encoding="utf-8") as f:
        for linha in f:
            try:
                e = json.loads(linha)
            except json.JSONDecodeError:
                continue
            e["_n"] = numero_tique(e["tique"]) if e.get("tique") else 0
            out.append(e)
    return out


def eventos(run: Path) -> list[dict]:
    p = run / "eventos.jsonl"
    return _ler_eventos(str(p), p.stat().st_mtime) if p.exists() else []


@st.cache_data(show_spinner=False)
def _ler_json(caminho: str, _mtime: float):
    return json.loads(Path(caminho).read_text(encoding="utf-8"))


def ler_json(p: Path):
    return _ler_json(str(p), p.stat().st_mtime) if p.exists() else None


def tiques_disponiveis(run: Path) -> list[int]:
    return sorted(int(p.stem) for p in (run / "tiques").glob("*.json"))


def foto(run: Path, n: int) -> dict:
    return ler_json(run / "tiques" / f"{n:03d}.json") or {}


@st.cache_data(show_spinner=False)
def _ler_llm(caminho: str, _mtime: float) -> list[dict]:
    return [json.loads(l) for l in Path(caminho).read_text(encoding="utf-8").splitlines() if l.strip()]


def chamadas_llm(run: Path, n: int) -> list[dict]:
    p = run / "llm" / f"{n:03d}.jsonl"
    return _ler_llm(str(p), p.stat().st_mtime) if p.exists() else []


def sql(run: Path, consulta: str, params: tuple = ()) -> list[dict]:
    p = run / "estado.sqlite"
    if not p.exists():
        return []
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True)
        con.row_factory = sqlite3.Row
        out = [dict(r) for r in con.execute(consulta, params)]
        con.close()
        return out
    except sqlite3.Error:
        return []


def brl(v) -> str:
    try:
        return "R$ " + f"{float(v):,.2f}".replace(",", "X").replace(".", ",").replace("X", ".")
    except (TypeError, ValueError):
        return "-"


DIAS = ["seg", "ter", "qua", "qui", "sex", "sáb", "dom"]


def hora(n: int) -> str:
    d = INICIO + timedelta(hours=n)
    return f"{DIAS[d.weekday()]} {d:%d/%m %H:%M}"


def resumo_evento(e: dict) -> str:
    t = e["tipo"]
    if t == "ferramenta":
        a = e.get("args") or {}
        r = e.get("resultado") or {}
        alvo = a.get("titulo_id") or a.get("fornecedor_id") or a.get("pessoa_id") or ""
        res = "ok" if r.get("ok") else (r.get("erro") or r.get("motivo") or ("requer " + ", ".join(r["requer"])
                                                                              if r.get("requer") else ""))
        return f"{e.get('ferramenta')} {alvo} → {str(res)[:120]}"
    if t == "llm_chamada":
        return f"{e.get('modelo')} · {e.get('latencia_s')} s · {e.get('tokens') or '?'} tokens · {e.get('contexto') or ''}"
    if t == "caso_aberto":
        return f"{e.get('caso_id')} · {len(e.get('titulos') or [])} título(s) · {brl(e.get('valor_em_jogo'))} · {e.get('origem')}"
    if t == "proposta":
        return f"{e.get('caso_id')} · p={e.get('p_problema')} · {(e.get('hipotese') or '')[:160]}"
    if t == "veredito":
        return f"{e.get('caso_id')} · {'reproduziu ✅' if e.get('reproduziu') else 'REFUTOU ❌'}"
    if t in ("critico_refutou", "critico_dispensado", "caso_adiado"):
        return f"{e.get('caso_id')} · {str(e.get('motivo') or e.get('objecoes') or '')[:160]}"
    if t == "card_escalada":
        return f"{e.get('card_id')} → {e.get('pessoa_id')} · {e.get('itens')} decisão(ões)"
    if t == "resposta_fornecedor":
        return f"{e.get('comunicacao_id')} · {e.get('intencao')} · {'respondeu' if e.get('respondeu') else 'não respondeu'}"
    if t == "leitura_e9":
        return f"{e.get('fornecedor_id')} · {len(e.get('comunicacoes') or [])} comunicação(ões)"
    if t == "lote_fechado":
        return f"{e.get('lote_remessa')} · {e.get('qtd_enviados')} enviados · {brl(e.get('valor_enviado'))}"
    if t == "plano":
        return f"{len(e.get('tarefas') or [])} tarefa(s) de {e.get('casos_pendentes')} caso(s)"
    if t == "tique_resumo":
        return f"{e.get('segundos')} s de relógio · {int((e.get('llm') or {}).get('chamadas') or 0)} chamadas de LLM"
    return json.dumps({k: v for k, v in e.items() if k not in ("ts_real", "tique", "ator", "tipo", "_n")},
                      ensure_ascii=False, default=str)[:180]


# ---------------------------------------------------------------------- controles do replay
runs = execucoes()
if not runs:
    st.error("Nenhuma execução com fotografias em runs/. Rode o orquestrador ou scripts/reconstruir_tiques.py.")
    st.stop()

with st.sidebar:
    st.header("🎬 Replay")
    nomes = [p.name for p in runs]
    run = RUNS / st.selectbox("Execução", nomes, index=0,
                              help="Pastas em runs/ com fotografias por tique (a mais recente primeiro).")
    ts = tiques_disponiveis(run)
    ev_all = eventos(run)
    rec = ler_json(run / "reconstrucao.json")
    em_andamento = not any(e["tipo"] == "tique_resumo" for e in ev_all[-3:]) or (
        time.time() - (run / "eventos.jsonl").stat().st_mtime < 120)
    if rec:
        st.caption("⚠️ Execução reconstruída (gravada antes da gravação nativa).")
    if em_andamento:
        st.caption("🔴 Execução em andamento: novas fotografias aparecem sozinhas.")
    if "n" not in st.session_state or st.session_state.get("_run") != run.name:
        st.session_state.n, st.session_state._run, st.session_state.play = ts[0], run.name, False
    if "_prox" in st.session_state:  # avanço pedido pelo play no rerun anterior
        st.session_state.n = st.session_state.pop("_prox")
    st.session_state.n = min(max(st.session_state.n, ts[0]), ts[-1])

    c1, c2, c3 = st.columns(3)
    if c1.button("⏮", width="stretch", help="tique anterior"):
        st.session_state.n = max(ts[0], st.session_state.n - 1)
    if c2.button("⏸" if st.session_state.play else "▶", width="stretch", help="play / pausa"):
        st.session_state.play = not st.session_state.play
    if c3.button("⏭", width="stretch", help="próximo tique"):
        st.session_state.n = min(ts[-1], st.session_state.n + 1)
    vel = st.select_slider("Velocidade (tiques por segundo)", [0.5, 1, 2, 4], value=1)
    if st.button("⏩ Pular para o próximo lote", width="stretch"):
        atual = foto(run, st.session_state.n).get("proximo_lote") or {}
        alvo = next((n for n in ts if n > st.session_state.n and
                     (foto(run, n).get("proximo_lote") or {}).get("lote_remessa") != atual.get("lote_remessa")), ts[-1])
        st.session_state.n = alvo
    abertos = [e for e in ev_all if e["tipo"] == "caso_aberto"]
    opcoes = ["—"] + [f"{e['caso_id']} · {hora(e['_n'])} · {brl(e.get('valor_em_jogo'))}" for e in abertos]
    escolha = st.selectbox("🎯 Ir para o caso", opcoes, help="Posiciona o replay no tique em que o caso começa.")
    if escolha != "—" and st.session_state.get("_caso_ir") != escolha:
        st.session_state._caso_ir = escolha
        st.session_state.caso_foco = escolha.split(" · ")[0]
        st.session_state.n = next(e["_n"] for e in abertos if e["caso_id"] == st.session_state.caso_foco)
    n = st.slider("Tique", ts[0], ts[-1], key="n")
    st.caption(f"{len(ts)} tiques gravados · {len(ev_all)} eventos")

F = foto(run, n)
EV = [e for e in ev_all if e["_n"] <= n]
EV_T = [e for e in ev_all if e["_n"] == n]
prox = F.get("proximo_lote") or {}

st.title("🧾 Time de agentes de contas a pagar")
m = F.get("metricas", {})
k = st.columns(6)
k[0].metric("Relógio simulado", hora(n))
k[1].metric("Próximo lote", prox.get("lote_remessa", "-"), f"fecha em {prox.get('faltam_h', '-')} h", delta_color="off")
lote_ts = F.get("titulos_proximo_lote", [])
k[2].metric("Títulos no lote", len(lote_ts), brl(sum(t["valor"] or 0 for t in lote_ts)), delta_color="off")
k[3].metric("Provado só em código", f"{m.get('pct_provado_so_codigo_lote') or 0}%")
k[4].metric("Casos abertos", m.get("casos_abertos", 0), f"{m.get('critico_refutou', 0)} refutações do Crítico",
            delta_color="off")
k[5].metric("Decisões levadas a pessoas", m.get("decisoes_levadas_a_pessoas", 0), f"{m.get('cards_humanos', 0)} card(s)",
            delta_color="off")
if F.get("faltaram"):
    with st.expander("O que esta execução não gravou (reconstruída)"):
        for f in F["faltaram"]:
            st.write("•", f)

abas = st.tabs(["⏱ Ao vivo", "🚦 Lote certificado", "🗂 Casos por mesa", "🧠 Aprendizado", "📊 Métricas e Tempo",
                "✋ Fila de aprovação"])

# ---------------------------------------------------------------------- 1. Ao vivo
with abas[0]:
    st.subheader(f"Tique {n} · {hora(n)}")
    res = next((e for e in EV_T if e["tipo"] == "tique_resumo"), None)
    if res:
        c = st.columns(5)
        c[0].metric("Tempo real do tique", f"{res.get('segundos')} s", f"{100 * (res.get('segundos') or 0) / 3600:.1f}% da hora",
                    delta_color="off")
        c[1].metric("Chamadas de LLM", int((res.get("llm") or {}).get("chamadas") or 0))
        c[2].metric("Leituras E9", res.get("leituras_e9"))
        c[3].metric("Casos investigados", res.get("investigados"), f"de {res.get('casos_pendentes')} pendentes",
                    delta_color="off")
        c[4].metric("Respostas a fornecedor", res.get("relacionamento"))
    filtro = st.multiselect("Filtrar tipos", sorted({e["tipo"] for e in EV_T}),
                            default=[t for t in sorted({e["tipo"] for e in EV_T}) if t not in ("llm_chamada", "reavaliacao")])
    linhas = [{"quando": e.get("ts_real", "")[11:], "agente": e["ator"], "o quê": e["tipo"], "detalhe": resumo_evento(e)}
              for e in EV_T if e["tipo"] in filtro]
    st.dataframe(pd.DataFrame(linhas), width="stretch", hide_index=True, height=480)

# ---------------------------------------------------------------------- 2. Lote certificado
with abas[1]:
    st.subheader(f"{prox.get('lote_remessa', '-')} · {prox.get('banco', '')} · fecha {prox.get('fecha_em', '')}")
    if not lote_ts:
        st.info("Sem lote aberto neste tique.")
    else:
        cont = Counter(t["resultado"] for t in lote_ts)
        c = st.columns(4)
        c[0].metric("🟢 Provado", cont.get("PROVADO", 0))
        c[1].metric("🟡 Duvidoso", cont.get("DUVIDOSO", 0))
        c[2].metric("🔴 Quebrado", cont.get("QUEBRADO", 0))
        fora = [t for t in lote_ts if t["estado"] not in SAIU]
        c[3].metric("Fora do lote (retido/bloqueado/aguardando)", len(fora), brl(sum(t["valor"] or 0 for t in fora)),
                    delta_color="off")
        so = st.radio("Mostrar", ["todos", "só com elo quebrado ou duvidoso", "só fora do lote"], horizontal=True)
        df = pd.DataFrame([{
            "": SEMAFORO.get(t["resultado"], "⚪"), "título": t["titulo_id"], "fornecedor": t["fornecedor_id"],
            "valor": t["valor"], "estado": t["estado"], "resultado": t["resultado"],
            "elos": " ".join(f"{e['elo']}{STATUS_ELO.get(e['status'], '?')}" for e in t["elos"]),
            "quebrados": ", ".join(t["quebrados"]), "caso": t["caso_id"] or ""} for t in lote_ts])
        if so.startswith("só com"):
            df = df[df["resultado"] != "PROVADO"]
        elif so.startswith("só fora"):
            df = df[~df["estado"].isin(SAIU)]
        st.dataframe(df, width="stretch", hide_index=True, height=380,
                     column_config={"valor": st.column_config.NumberColumn(format="R$ %.2f")})
        sel = st.selectbox("Ver a cadeia de um título", ["—"] + list(df["título"]))
        if sel != "—":
            t = next(x for x in lote_ts if x["titulo_id"] == sel)
            st.markdown(f"**{sel}** · {brl(t['valor'])} · estado `{t['estado']}` · {SEMAFORO.get(t['resultado'], '')} "
                        f"{t['resultado']}" + (f" · caso **{t['caso_id']}**" if t["caso_id"] else ""))
            st.dataframe(pd.DataFrame([{"elo": f"{e['elo']} {e['nome']}", "status": f"{STATUS_ELO.get(e['status'], '')} "
                                        f"{e['status']}", "explicação": e["explicacao"],
                                        "evidências": ", ".join(map(str, e["evidencias"]))} for e in t["elos"]]),
                         width="stretch", hide_index=True)

# ---------------------------------------------------------------------- 3. Casos por mesa
with abas[2]:
    casos = F.get("casos", [])
    mesa1, mesa2, mesa3 = st.tabs([f"Integridade ({len(casos)})", "Pontualidade", "Conciliação e relacionamento"])
    with mesa1:
        if not casos:
            st.info("Nenhum caso aberto até este tique.")
        else:
            st.dataframe(pd.DataFrame([{"caso": c["id"], "status": c["status"], "fornecedor": c["fornecedor_id"],
                                        "títulos": len(c["titulos"]), "em jogo": c["valor_em_jogo"], "prazo": c["prazo"],
                                        "origem": c["origem"], "p(problema)": c["p_problema"],
                                        "desfecho": c["desfecho"]} for c in casos]),
                         width="stretch", hide_index=True, height=260,
                         column_config={"em jogo": st.column_config.NumberColumn(format="R$ %.2f")})
            ids = [c["id"] for c in casos]
            foco = st.session_state.get("caso_foco")
            cid = st.selectbox("Abrir caso", ids, index=ids.index(foco) if foco in ids else 0)
            c = next(x for x in casos if x["id"] == cid)
            evc = [e for e in EV if e.get("caso_id") == cid]
            st.markdown(f"### {cid} · {c['fornecedor_id']} · {brl(c['valor_em_jogo'])} · prazo {c['prazo']}")
            st.caption(f"Títulos: {', '.join(c['titulos'])} · origem: {c['origem']}")
            for e in evc:
                st.markdown(f"**{hora(e['_n'])} · {e['ator']} · {e['tipo']}**")
                if e["tipo"] == "proposta":
                    st.write(e.get("hipotese"))
                    st.caption(f"p(problema) = {e.get('p_problema')} · evidências: {', '.join(map(str, e.get('evidencias') or []))}")
                    if e.get("acoes"):
                        st.write("Ação proposta:", e["acoes"])
                    if e.get("custo_esperado_politica"):
                        st.write("Custo esperado (política):", e["custo_esperado_politica"])
                elif e["tipo"] == "veredito":
                    st.write("✅ Crítico reproduziu" if e.get("reproduziu") else "❌ Crítico NÃO reproduziu")
                    if e.get("objecoes"):
                        st.write("Objeções:", e["objecoes"])
                    st.caption(f"evidências próprias: {', '.join(map(str, e.get('evidencias_proprias') or []))}")
                else:
                    st.caption(resumo_evento(e))
            titulos_c = set(c["titulos"])
            acoes = [e for e in EV if e["tipo"] == "ferramenta" and e["ator"] in ("executor", "watchdog", "pontualidade")
                     and (e.get("args") or {}).get("titulo_id") in titulos_c]
            if acoes:
                st.markdown("**Ações pelo guarda**")
                st.dataframe(pd.DataFrame([{"quando": hora(e["_n"]), "quem": e["ator"], "ação": resumo_evento(e)}
                                           for e in acoes]), width="stretch", hide_index=True)
            pensou = [r for k2 in sorted({e["_n"] for e in evc}) for r in chamadas_llm(run, k2)
                      if r.get("contexto") == cid]
            with st.expander(f"🧠 O que o agente pensou ({len(pensou)} chamadas de LLM)"):
                for r in pensou:
                    st.markdown(f"**{r.get('agente')}** · `{r.get('modelo')}` · {r.get('latencia_s')} s")
                    if r.get("resposta"):
                        st.json(r["resposta"], expanded=False)
                    elif r.get("mensagens") is None:
                        st.caption("conteúdo não gravado nesta execução (reconstruída)")
    with mesa2:
        pont = [e for e in EV if e["ator"] == "pontualidade" or e["tipo"] == "regra_do_prazo"
                or (e["tipo"] == "ferramenta" and e["ator"] == "watchdog")]
        st.caption("Mesa 2: o legítimo volta ao lote a tempo; a regra do prazo age quando o lote fecha com título não provado.")
        if pont:
            st.dataframe(pd.DataFrame([{"quando": hora(e["_n"]), "quem": e["ator"], "o quê": resumo_evento(e)} for e in pont]),
                         width="stretch", hide_index=True)
        else:
            st.info("Nenhuma ação da Pontualidade até este tique.")
    with mesa3:
        rel = [e for e in EV if e["tipo"] in ("resposta_fornecedor", "nao_responde", "encaminhamento_adiado")]
        st.caption("Mesa 3: responde sozinha ao fornecedor, só pelo canal do cadastro, com fatos do ERP e do banco.")
        if rel:
            st.dataframe(pd.DataFrame([{"quando": hora(e["_n"]), "comunicação": e.get("comunicacao_id"),
                                        "fornecedor": e.get("fornecedor_id"), "intenção": e.get("intencao") or e["tipo"],
                                        "respondeu": "sim" if e.get("respondeu") else "não",
                                        "encaminhado a": e.get("encaminhado") or "",
                                        "motivo": e.get("motivo") or ""} for e in rel]),
                         width="stretch", hide_index=True, height=300)
            com_texto = [e for e in rel if e.get("texto")]
            if com_texto:
                sel = st.selectbox("Ler resposta enviada", [e["comunicacao_id"] for e in com_texto])
                st.text(next(e["texto"] for e in com_texto if e["comunicacao_id"] == sel))
        else:
            st.info("Nenhuma mensagem de fornecedor tratada até este tique.")

# ---------------------------------------------------------------------- 4. Aprendizado
with abas[3]:
    dets = F.get("detectores", [])
    cur_dir = run.parent / f"{run.name}_curadoria"
    cur = ler_json(cur_dir / "curadoria.json")
    c = st.columns(3)
    c[0].metric("Detectores criados pelos agentes", len(dets))
    if cur:
        c[1].metric("Ativos depois da curadoria", cur["ativos_depois"], f"{cur['ativos_antes'] - cur['ativos_depois']} a menos",
                    delta_color="off")
        c[2].metric("Consolidados / rebaixados", f"{len(cur['consolidados'])} / {len(cur['rebaixados'])}")
    if dets:
        info = {d["id"]: d for d in (cur["detectores"] if cur else [])}
        st.dataframe(pd.DataFrame([{
            "detector": d["id"], "nome": d["nome"], "criado": d["criado_no_tique"], "caso de origem": d["caso_origem"],
            "alertas": d["n_alertas"], "confirmados": d["n_confirmados"], "falsos": d["n_falsos"],
            "precisão": (f"{d['n_confirmados'] / (d['n_confirmados'] + d['n_falsos']):.0%}"
                         if (d["n_confirmados"] or 0) + (d["n_falsos"] or 0) else "-"),
            "depois da curadoria": ("ativo" if info.get(d["id"], {}).get("ativo") else
                                    (info.get(d["id"], {}).get("motivo_status") or "")) if cur else
            ("ativo" if d["ativo"] else d["motivo_status"])} for d in dets]),
            width="stretch", hide_index=True)
    fonte = cur_dir if cur else run
    lic = sql(fonte, "SELECT texto, origem, quando FROM licoes ORDER BY quando")
    prop = sql(fonte, "SELECT processo, descricao, quando, status FROM propostas_processo ORDER BY quando")
    aut = sql(fonte, "SELECT tipo_de_caso, n_aprovadas, proposta, status FROM propostas_autonomia")
    st.markdown("#### Lições")
    st.dataframe(pd.DataFrame(lic or [{"texto": "nenhuma ainda"}]), width="stretch", hide_index=True)
    st.markdown("#### Propostas de correção de processo")
    st.dataframe(pd.DataFrame(prop or [{"descricao": "nenhuma ainda"}]), width="stretch", hide_index=True)
    st.markdown("#### Autonomia conquistada")
    st.dataframe(pd.DataFrame(aut or [{"proposta": "nenhum tipo de escalada com 3 aprovações seguidas ainda"}]),
                 width="stretch", hide_index=True)

# ---------------------------------------------------------------------- 5. Métricas e Tempo
with abas[4]:
    if (run / "resumo.md").exists():
        with st.expander("🏁 Placar da execução inteira (resumo.md: atraso por obrigação, bug × decisão, lotes futuros)",
                         expanded=False):
            st.markdown((run / "resumo.md").read_text(encoding="utf-8"))
    c = st.columns(4)
    c[0].metric("Propostas / vereditos", f"{m.get('propostas', 0)} / {m.get('vereditos', 0)}")
    c[1].metric("Crítico: refutou / dispensado", f"{m.get('critico_refutou', 0)} / {m.get('critico_dispensado', 0)}")
    c[2].metric("Pedidos de aprovação (guarda)", m.get("pedidos_de_aprovacao", 0))
    c[3].metric("Tiques sem LLM", f"{m.get('tiques_sem_llm', 0)} de {m.get('tiques', 0)}")
    rm = m.get("retido_por_motivo", {})
    if rm:
        st.markdown("#### R$ fora do lote por motivo (elo quebrado), neste tique")
        st.dataframe(pd.DataFrame([{"motivo": k2, "títulos": v["qtd"], "valor": v["valor"]} for k2, v in rm.items()])
                     .sort_values("valor", ascending=False), width="stretch", hide_index=True,
                     column_config={"valor": st.column_config.NumberColumn(format="R$ %.2f")})
    saude = F.get("saude_cadeia", [])
    if saude:
        st.markdown("#### Saúde da cadeia por elo e por fonte")
        st.dataframe(pd.DataFrame(saude)[["tipo", "chave", "n", "ok", "quebrado", "inconclusivo", "pct_ok"]],
                     width="stretch", hide_index=True)

    st.markdown("### ⏱ Tempo: simulado × real × projeção")
    resumos = [e for e in ev_all if e["tipo"] == "tique_resumo"]
    if resumos:
        llm_ev = [e for e in ev_all if e["tipo"] == "llm_chamada"]
        por_t = defaultdict(list)
        for e in llm_ev:
            por_t[e["_n"]].append(e)
        N = st.slider("Premissa da projeção: chamadas simultâneas sem fila", 2, 8, 4)
        linhas = []
        for r in resumos:
            t = r["_n"]
            cham = por_t.get(t, [])
            soma = sum(c2.get("latencia_s") or 0 for c2 in cham)
            por_caso = defaultdict(float)
            fora = 0.0
            for c2 in cham:
                ctx = c2.get("contexto") or ""
                if ctx.startswith("CASO"):
                    por_caso[ctx] += c2.get("latencia_s") or 0
                else:
                    fora += c2.get("latencia_s") or 0
            caminho = max(por_caso.values(), default=0.0)
            proj = max(caminho, sum(por_caso.values()) / N) + fora / N
            linhas.append({"tique": t, "hora": hora(t), "real (s)": r.get("segundos"),
                           "% da hora": round(100 * (r.get("segundos") or 0) / 3600, 1),
                           "folga (min)": round((3600 - (r.get("segundos") or 0)) / 60, 1),
                           "chamadas": len(cham), "soma latências (s)": round(soma, 1),
                           "casos": len(por_caso), "projeção paralela (s)": round(proj, 1)})
        dt = pd.DataFrame(linhas)
        c = st.columns(4)
        c[0].metric("Tique mais pesado", f"{dt['real (s)'].max():.0f} s", f"{dt['% da hora'].max()}% da hora",
                    delta_color="off")
        c[1].metric("Tique médio", f"{dt['real (s)'].mean():.0f} s")
        c[2].metric("Tempo real total", f"{dt['real (s)'].sum() / 60:.0f} min", f"para {len(dt)} h simuladas",
                    delta_color="off")
        c[3].metric("Projeção paralela total", f"{dt['projeção paralela (s)'].sum() / 60:.0f} min",
                    f"premissa: {N} simultâneas", delta_color="off")
        st.bar_chart(dt.set_index("tique")[["real (s)", "projeção paralela (s)"]])
        st.caption(f"Cada tique representa 1 hora da vida real (3.600 s). Projeção = max(caso mais longo do tique, soma dos "
                   f"casos ÷ {N}) + chamadas fora de casos ÷ {N}, com **{N} chamadas simultâneas sem fila no proxy**. "
                   f"É estimativa a partir das latências gravadas, não medição. A cadeia (código) confere o lote inteiro "
                   f"em ~1 s; o tempo está nos casos que sobem para LLM.")
        st.dataframe(dt, width="stretch", hide_index=True)
        pp = defaultdict(lambda: {"chamadas": 0, "segundos": 0.0})
        pm = defaultdict(lambda: {"chamadas": 0, "segundos": 0.0})
        for e in llm_ev:
            pp[e["ator"]]["chamadas"] += 1
            pp[e["ator"]]["segundos"] += e.get("latencia_s") or 0
            pm[e.get("modelo")]["chamadas"] += 1
            pm[e.get("modelo")]["segundos"] += e.get("latencia_s") or 0
        c1, c2 = st.columns(2)
        c1.markdown("**Por papel**")
        c1.dataframe(pd.DataFrame([{"papel": k2, "chamadas": v["chamadas"], "segundos": round(v["segundos"]),
                                    "média (s)": round(v["segundos"] / v["chamadas"], 1)} for k2, v in pp.items()])
                     .sort_values("segundos", ascending=False), width="stretch", hide_index=True)
        c2.markdown("**Por modelo**")
        c2.dataframe(pd.DataFrame([{"modelo": k2, "chamadas": v["chamadas"], "segundos": round(v["segundos"]),
                                    "média (s)": round(v["segundos"] / v["chamadas"], 1)} for k2, v in pm.items()])
                     .sort_values("segundos", ascending=False), width="stretch", hide_index=True)
        st.caption("Pessoa × agente: sem uma premissa de tempo humano por título conferido, este painel não inventa essa comparação.")

# ---------------------------------------------------------------------- 6. Fila de aprovação
with abas[5]:
    st.caption("Espelho do Telegram e plano B: um card por pessoa e lote, só com decisões que a política reserva a pessoas. "
               "No replay os botões ficam desativados (a decisão já aconteceu ou não aconteceu na execução).")
    cards = F.get("cards", [])
    if not cards:
        st.info("Nenhum card até este tique.")
    for cd in reversed(cards):
        with st.container(border=True):
            st.markdown(f"**{cd['id']}** · para `{cd['pessoa_id']}` · lote {cd['lote_remessa'] or 'sem lote'} · "
                        f"{cd['criado_em']} · {cd['status'] or ''}")
            st.text(cd["texto"] or "")
            b = st.columns(3)
            b[0].button("✅ Aprovar", key=f"a{cd['id']}", disabled=True)
            b[1].button("❌ Recusar", key=f"r{cd['id']}", disabled=True)
            b[2].button("🔎 Pedir evidência", key=f"p{cd['id']}", disabled=True)

# ---------------------------------------------------------------------- play / execução em andamento
if st.session_state.play:
    time.sleep(1 / vel)
    if n < ts[-1]:
        st.session_state._prox = n + 1
    elif not em_andamento:
        st.session_state.play = False
    st.rerun()
elif em_andamento and n == ts[-1]:
    time.sleep(10)  # acompanha a execução: recarrega para pegar a próxima fotografia
    st.rerun()
